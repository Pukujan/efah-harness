"""``evaluation_graph``, ``deployment_graph``, ``closeout_graph`` -- Section 10.2.

These three close the lifecycle. WS-E owns the assurance lane proper (Inspect AI,
mutants, holdouts) and WS-F owns deployment surfaces; what belongs to the runtime
is the *control flow*: which gates are outstanding, whether the environment is
reachable, and which terminal :class:`~governance.states.ProjectState` the run
ends in.

``closeout_graph`` is where the contract's hardest rule about honesty lives.
Section 6.2 lists the only states that end a run, and ``autonomy-policy.yaml``
names the things that are explicitly *not* terminal -- "worker completed a task",
"pr opened", "visible tests passed", "mostly done". Outstanding gates therefore
produce ``FAILED_ASSURANCE``, never a cheerful summary.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from governance.envelope import content_hash
from governance.states import ProjectState, Verdict
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

EVALUATION_GRAPH_ID = "evaluation_graph"
DEPLOYMENT_GRAPH_ID = "deployment_graph"
CLOSEOUT_GRAPH_ID = "closeout_graph"


def build_evaluation_graph(services: WorkflowServices) -> StateGraph:
    """START -> load_gate_definitions -> evaluate_deterministic -> record -> END.

    Only gates whose ``oracle_type`` is deterministic and whose
    ``model_judge_in_verdict_path`` is false are decided here. Anything else is
    left ``UNVERIFIABLE`` for WS-E's evaluation runtime -- GATE-D2-20 exists
    precisely to keep a judge out of the deterministic path, and a runtime that
    guessed on its behalf would be the violation.
    """
    builder = StateGraph(WorkflowState)

    @node(EVALUATION_GRAPH_ID, "load_gate_definitions", services)
    def load_gate_definitions(state: WorkflowState) -> dict[str, Any]:
        gates = services.pack.acceptance_gates()
        pending = list(state.get("pending_gates", [])) or sorted(gates)
        return {
            "pending_gates": pending,
            "artifacts": {
                "gate_definitions": {
                    gid: {
                        "oracle_type": gates[gid].get("oracle_type"),
                        "judge_in_path": bool(gates[gid].get("model_judge_in_verdict_path")),
                        "blocking": bool(gates[gid].get("blocking")),
                    }
                    for gid in pending
                    if gid in gates
                }
            },
        }

    @node(EVALUATION_GRAPH_ID, "evaluate_deterministic_gates", services)
    def evaluate_deterministic_gates(state: WorkflowState) -> dict[str, Any]:
        definitions = state.get("artifacts", {}).get("gate_definitions", {})
        produced = set(state.get("output_hashes", {}))
        verdicts: dict[str, Any] = {}
        for gate_id, meta in definitions.items():
            if meta.get("judge_in_path"):
                verdicts[gate_id] = {"verdict": str(Verdict.UNVERIFIABLE), "reason": "judge_not_in_runtime_path"}
                continue
            # The runtime can only assert what it can see: evidence produced by
            # this run. Absence of evidence is UNVERIFIABLE, not PASS.
            verdicts[gate_id] = {
                "verdict": str(Verdict.UNVERIFIABLE),
                "reason": "no_run_evidence" if not produced else "awaiting_assurance_lane",
                "evidence_hashes": sorted(produced),
            }
        return {"gate_verdicts": verdicts}

    @node(EVALUATION_GRAPH_ID, "record_evaluation", services)
    def record_evaluation(state: WorkflowState) -> dict[str, Any]:
        verdicts = {k: v for k, v in state.get("gate_verdicts", {}).items() if k.startswith("GATE-")}
        return {"output_hashes": {"evaluation.verdicts": content_hash(verdicts)}}

    builder.add_node("load_gate_definitions", load_gate_definitions)
    builder.add_node("evaluate_deterministic_gates", evaluate_deterministic_gates)
    builder.add_node("record_evaluation", record_evaluation)
    builder.add_edge(START, "load_gate_definitions")
    builder.add_edge("load_gate_definitions", "evaluate_deterministic_gates")
    builder.add_edge("evaluate_deterministic_gates", "record_evaluation")
    builder.add_edge("record_evaluation", END)
    return builder


def build_deployment_graph(services: WorkflowServices) -> StateGraph:
    """START -> resolve_environment -> check_release_preconditions -> END.

    Section 21.2 gives the auto-merge preconditions and ``merge_authority`` says
    the implementing agent may not self-certify. This graph therefore reports
    readiness; it never performs a merge.
    """
    builder = StateGraph(WorkflowState)

    @node(DEPLOYMENT_GRAPH_ID, "resolve_environment", services)
    def resolve_environment(state: WorkflowState) -> dict[str, Any]:
        envs = services.pack.yaml("environments.yaml")
        name = envs.get("default_environment", "dev")
        env = envs.get("environments", {}).get(name, {})
        services_named = sorted(k for k, v in env.items() if isinstance(v, dict))
        return {
            "artifacts": {"environment": name, "environment_services": services_named},
            "output_hashes": {"deployment.environment": content_hash({"name": name, "services": services_named})},
        }

    @node(DEPLOYMENT_GRAPH_ID, "check_release_preconditions", services)
    def check_release_preconditions(state: WorkflowState) -> dict[str, Any]:
        policy = services.pack.yaml("autonomy-policy.yaml").get("auto_merge_requirements", {})
        blockers = list(state.get("typed_blockers", []))
        unresolved = sorted(
            {
                gid
                for gid, verdict in state.get("gate_verdicts", {}).items()
                if gid.startswith("GATE-") and isinstance(verdict, dict) and verdict.get("verdict") != str(Verdict.PASS)
            }
        )
        ready = not blockers and not unresolved
        return {
            "artifacts": {
                "auto_merge_requirements": sorted(policy),
                "release_ready": ready,
                "release_blockers": blockers + unresolved,
                # Section 21.2 merge_authority: never the implementing agent.
                "merge_performed_by": "ci_service_identity",
            }
        }

    builder.add_node("resolve_environment", resolve_environment)
    builder.add_node("check_release_preconditions", check_release_preconditions)
    builder.add_edge(START, "resolve_environment")
    builder.add_edge("resolve_environment", "check_release_preconditions")
    builder.add_edge("check_release_preconditions", END)
    return builder


def build_closeout_graph(services: WorkflowServices) -> StateGraph:
    """START -> collect_evidence -> determine_terminal_state -> END."""
    builder = StateGraph(WorkflowState)

    @node(CLOSEOUT_GRAPH_ID, "collect_evidence", services)
    def collect_evidence(state: WorkflowState) -> dict[str, Any]:
        evidence = {
            "input_hashes": state.get("input_hashes", {}),
            "output_hashes": state.get("output_hashes", {}),
            "pending_gates": state.get("pending_gates", []),
            "typed_blockers": state.get("typed_blockers", []),
            "node_log": state.get("node_log", []),
        }
        return {
            "artifacts": {"evidence_package": evidence},
            "output_hashes": {"closeout.evidence": content_hash(evidence)},
        }

    @node(CLOSEOUT_GRAPH_ID, "determine_terminal_state", services)
    def determine_terminal_state(state: WorkflowState) -> dict[str, Any]:
        """Section 6.2: only these end a run, and "mostly done" is not one."""
        if state.get("owner_interrupts"):
            return {"project_state": str(ProjectState.BLOCKED_OWNER_DECISION)}
        if state.get("typed_blockers"):
            return {"project_state": str(ProjectState.FAILED_ASSURANCE)}
        outstanding = [
            gid
            for gid in state.get("pending_gates", [])
            if state.get("gate_verdicts", {}).get(gid, {}).get("verdict") != str(Verdict.PASS)
        ]
        if outstanding:
            return {
                "project_state": str(ProjectState.FAILED_ASSURANCE),
                "artifacts": {"outstanding_gates": outstanding},
            }
        return {"project_state": str(ProjectState.VERIFIED_COMPLETE)}

    builder.add_node("collect_evidence", collect_evidence)
    builder.add_node("determine_terminal_state", determine_terminal_state)
    builder.add_edge(START, "collect_evidence")
    builder.add_edge("collect_evidence", "determine_terminal_state")
    builder.add_edge("determine_terminal_state", END)
    return builder
