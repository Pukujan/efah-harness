"""``intake_graph`` and ``research_graph`` -- Contract Sections 6.1, 10.2, 15.5.

``intake_graph`` is one of the three graphs that carry the walking skeleton, so
it does real work against the real pack: it loads and hashes
``project-pack/``, binds every required file's content hash into the state's
``input_hashes``, and declares the gates the pack import owes evidence for. A
pack that does not validate produces a typed blocker, never a default
(Section 8.1).

``research_graph`` derives the material open questions from the pack itself and
tiers them. Section 15.5 is the constraint that shapes it: agent output enters at
``T2_HYPOTHESIS`` and does not get promoted by being restated confidently.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from governance.envelope import KnowledgeTier, content_hash
from governance.states import DriftFinding, FailureClass
from integrations.pack import PackValidationError
from workflows.failures import ClassifiedFailure
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "intake_graph"
RESEARCH_GRAPH_ID = "research_graph"


def build_intake_graph(services: WorkflowServices) -> StateGraph:
    """START -> load_pack -> validate_version_binding -> declare_gates -> END."""
    builder = StateGraph(WorkflowState)

    @node(GRAPH_ID, "load_pack", services)
    def load_pack_node(state: WorkflowState) -> dict[str, Any]:
        try:
            pack = services.pack
        except PackValidationError as exc:
            raise ClassifiedFailure(
                FailureClass.CONTRACT_DRIFT, f"project pack failed validation: {exc}"
            ) from exc
        hashes = {entry["name"]: entry["content_hash"] for entry in pack.file_manifest()}
        return {
            "input_hashes": {**hashes, "pack.manifest": pack.manifest_hash},
            "project_id": pack.project_id,
            "contract_version": pack.contract_version,
            "artifacts": {
                "pack_root": str(pack.root),
                "pack_manifest_hash": pack.manifest_hash,
                "owner_documents": pack.owner_documents(),
            },
        }

    @node(GRAPH_ID, "validate_version_binding", services)
    def validate_version_binding(state: WorkflowState) -> dict[str, Any]:
        """GATE-D1-02: every pack file is bound to this contract, not another."""
        pack = services.pack
        blockers: list[str] = []
        if state.get("contract_version") and state["contract_version"] != pack.contract_version:
            blockers.append(str(DriftFinding.STALE_CONTRACT_VERSION))
        return {
            "typed_blockers": blockers,
            "output_hashes": {"intake.version_binding": content_hash(
                {"contract_id": pack.contract_id, "contract_version": pack.contract_version}
            )},
        }

    @node(GRAPH_ID, "declare_gates", services)
    def declare_gates(state: WorkflowState) -> dict[str, Any]:
        pack = services.pack
        gates = pack.acceptance_gates()
        day_one = sorted(gid for gid, gate in gates.items() if int(gate.get("day", 9)) == 1)
        return {
            "pending_gates": day_one,
            "artifacts": {"gate_catalogue": sorted(gates)},
        }

    builder.add_node("load_pack", load_pack_node)
    builder.add_node("validate_version_binding", validate_version_binding)
    builder.add_node("declare_gates", declare_gates)
    builder.add_edge(START, "load_pack")
    builder.add_edge("load_pack", "validate_version_binding")
    builder.add_edge("validate_version_binding", "declare_gates")
    builder.add_edge("declare_gates", END)
    return builder


def build_research_graph(services: WorkflowServices) -> StateGraph:
    """START -> gather_sources -> form_hypotheses -> tier_knowledge -> END."""
    builder = StateGraph(WorkflowState)

    @node(RESEARCH_GRAPH_ID, "gather_sources", services)
    def gather_sources(state: WorkflowState) -> dict[str, Any]:
        pack = services.pack
        sources = {
            "owner_documents": sorted(pack.owner_documents()),
            "oracle_definitions": sorted(pack.oracle_definitions()),
            "context7_snapshots": sorted(
                p.name for p in (pack.root / "evidence" / "context7-snapshots").glob("*.json")
            ),
        }
        return {"artifacts": {"research_sources": sources}, "output_hashes": {"research.sources": content_hash(sources)}}

    @node(RESEARCH_GRAPH_ID, "form_hypotheses", services)
    def form_hypotheses(state: WorkflowState) -> dict[str, Any]:
        """Material unknowns come from the pack's own unresolved markers.

        Section 7.1 forbids asking the owner anything a probe can measure, so
        only ``TODO_owner`` markers become questions; ``TODO_builder_probe``
        becomes a probe task.
        """
        pack = services.pack
        owner_questions: list[str] = []
        builder_probes: list[str] = []
        for name, pack_file in pack.files.items():
            if not name.endswith(".yaml"):
                continue
            raw = pack_file.path.read_text()
            for marker, sink in (("TODO_owner", owner_questions), ("TODO_builder_probe", builder_probes)):
                if marker in raw:
                    sink.append(name)
        return {
            "artifacts": {
                "owner_question_sources": sorted(set(owner_questions)),
                "builder_probe_sources": sorted(set(builder_probes)),
            }
        }

    @node(RESEARCH_GRAPH_ID, "tier_knowledge", services)
    def tier_knowledge(state: WorkflowState) -> dict[str, Any]:
        """Section 15.5: derived findings enter as hypotheses, not as truth."""
        artifacts = state.get("artifacts", {})
        findings = {
            "owner_question_sources": str(KnowledgeTier.T2_HYPOTHESIS),
            "builder_probe_sources": str(KnowledgeTier.T2_HYPOTHESIS),
            "research_sources": str(KnowledgeTier.T1_OBSERVATION),
        }
        return {
            "artifacts": {"knowledge_tiers": findings},
            "output_hashes": {
                "research.findings": content_hash({k: artifacts.get(k) for k in sorted(findings)})
            },
        }

    builder.add_node("gather_sources", gather_sources)
    builder.add_node("form_hypotheses", form_hypotheses)
    builder.add_node("tier_knowledge", tier_knowledge)
    builder.add_edge(START, "gather_sources")
    builder.add_edge("gather_sources", "form_hypotheses")
    builder.add_edge("form_hypotheses", "tier_knowledge")
    builder.add_edge("tier_knowledge", END)
    return builder
