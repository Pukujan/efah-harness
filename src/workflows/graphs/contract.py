"""``contract_graph`` and ``contract_revalidation_graph`` -- Sections 10.2, 19.4.

``contract_graph`` freezes the governing contract: it binds the exact contract
document hash the run will be judged against and enumerates the acceptance
checks that must be evidenced. Freezing means recording the hash, not copying
the text -- Section 1.2 puts the contract above the harness, so the harness may
read it and must never rewrite it.

``contract_revalidation_graph`` is the Section 19.4 periodic and event-triggered
review. Its outcomes are the closed
:class:`~governance.states.ContractReviewOutcome` set; only
``CONTRACT_REAFFIRMED`` advances automatically.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from governance.envelope import content_hash
from governance.states import ContractReviewOutcome, DriftFinding, OwnerInterrupt
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "contract_graph"
REVALIDATION_GRAPH_ID = "contract_revalidation_graph"


def build_contract_graph(services: WorkflowServices) -> StateGraph:
    builder = StateGraph(WorkflowState)

    @node(GRAPH_ID, "freeze_contract", services)
    def freeze_contract(state: WorkflowState) -> dict[str, Any]:
        pack = services.pack
        frozen = {
            "contract_id": pack.contract_id,
            "contract_version": pack.contract_version,
            "contract_md_hash": pack.files["contract.md"].content_hash,
            "contract_yaml_hash": pack.files["contract.yaml"].content_hash,
        }
        return {
            "contract_version": pack.contract_version,
            "artifacts": {"frozen_contract": frozen},
            "output_hashes": {"contract.frozen": content_hash(frozen)},
        }

    @node(GRAPH_ID, "enumerate_acceptance_checks", services)
    def enumerate_acceptance_checks(state: WorkflowState) -> dict[str, Any]:
        checks = [str(c) for c in services.pack.yaml("contract.yaml").get("acceptance_checks", [])]
        return {
            "artifacts": {"acceptance_checks": checks},
            "output_hashes": {"contract.acceptance_checks": content_hash(checks)},
        }

    @node(GRAPH_ID, "bind_gates_to_checks", services)
    def bind_gates_to_checks(state: WorkflowState) -> dict[str, Any]:
        """An acceptance check with no visible gate is an unverifiable claim."""
        from planning.decomposition import unverified_checks

        orphans = unverified_checks(services.pack)
        return {
            "artifacts": {"acceptance_checks_without_gate": orphans},
            "typed_blockers": [str(DriftFinding.MISSING_WIRING)] if orphans else [],
        }

    builder.add_node("freeze_contract", freeze_contract)
    builder.add_node("enumerate_acceptance_checks", enumerate_acceptance_checks)
    builder.add_node("bind_gates_to_checks", bind_gates_to_checks)
    builder.add_edge(START, "freeze_contract")
    builder.add_edge("freeze_contract", "enumerate_acceptance_checks")
    builder.add_edge("enumerate_acceptance_checks", "bind_gates_to_checks")
    builder.add_edge("bind_gates_to_checks", END)
    return builder


def build_contract_revalidation_graph(services: WorkflowServices) -> StateGraph:
    """START -> rehash -> compare -> classify_outcome -> END.

    Section 19.4: the review is triggered periodically (every
    ``contract_review_interval_phases``) and on events. It compares the frozen
    hash against the live pack; a difference is ``DRIFT_DETECTED``, not an
    automatic re-freeze.
    """
    builder = StateGraph(WorkflowState)

    @node(REVALIDATION_GRAPH_ID, "rehash_contract", services)
    def rehash_contract(state: WorkflowState) -> dict[str, Any]:
        pack = services.pack
        live = {
            "contract_id": pack.contract_id,
            "contract_version": pack.contract_version,
            "contract_md_hash": pack.files["contract.md"].content_hash,
            "contract_yaml_hash": pack.files["contract.yaml"].content_hash,
        }
        return {"artifacts": {"live_contract": live}}

    @node(REVALIDATION_GRAPH_ID, "classify_outcome", services)
    def classify_outcome(state: WorkflowState) -> dict[str, Any]:
        artifacts = state.get("artifacts", {})
        frozen = artifacts.get("frozen_contract")
        live = artifacts.get("live_contract", {})
        if frozen is None:
            outcome = ContractReviewOutcome.EVIDENCE_STALE
        elif frozen == live:
            outcome = ContractReviewOutcome.CONTRACT_REAFFIRMED
        elif frozen.get("contract_version") != live.get("contract_version"):
            outcome = ContractReviewOutcome.AMENDMENT_REQUIRED
        else:
            outcome = ContractReviewOutcome.DRIFT_DETECTED

        blockers: list[str] = []
        interrupts: list[str] = []
        if outcome is ContractReviewOutcome.AMENDMENT_REQUIRED:
            # Section 10.7 permits exactly this interrupt type.
            interrupts.append(str(OwnerInterrupt.CONTRACT_AMENDMENT_REQUIRED))
        if outcome is ContractReviewOutcome.DRIFT_DETECTED:
            blockers.append(str(DriftFinding.STALE_CONTRACT_VERSION))

        return {
            "artifacts": {"contract_review_outcome": str(outcome)},
            "typed_blockers": blockers,
            "owner_interrupts": interrupts,
            "output_hashes": {"contract.review": content_hash({"outcome": str(outcome), "live": live})},
        }

    builder.add_node("rehash_contract", rehash_contract)
    builder.add_node("classify_outcome", classify_outcome)
    builder.add_edge(START, "rehash_contract")
    builder.add_edge("rehash_contract", "classify_outcome")
    builder.add_edge("classify_outcome", END)
    return builder
