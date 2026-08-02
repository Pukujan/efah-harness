"""``build_graph`` and ``task_graph`` -- Contract Sections 9.3, 9.5, 10.2.

``task_graph`` is the second of the three walking-skeleton graphs and the place
where WS-C's two halves meet: it acquires a real
:class:`~assignments.leases.AssignmentLease`, does the work unit's work, and
submits the result through :class:`~assignments.fencing.SubmissionGateway`, which
runs ORACLE-002 before anything is applied.

Two contract rules are enforced here rather than assumed:

* Section 9.3 -- "Workers may submit ``CANDIDATE_COMPLETE``. Only gates may
  produce ``PASSED``." The task graph never writes ``PASSED``; the best outcome
  it can reach is ``CANDIDATE_COMPLETE`` with pending gates attached.
* Section 9.5 -- a submission whose lease has expired or been superseded is
  rejected as ``STALE_ASSIGNMENT``. The graph does not decide that itself; it
  asks the oracle and records the verdict, including against its own work.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from assignments.fencing import LeaseFencingOracle, Submission, SubmissionGateway
from assignments.leases import LeaseError, OwnershipMode
from governance.envelope import content_hash
from governance.states import FailureClass, TaskState, Verdict
from planning.decomposition import WorkUnit, decompose
from workflows.failures import ClassifiedFailure, idempotency_key
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "build_graph"
TASK_GRAPH_ID = "task_graph"

#: Section 9.5 "permitted output schemas". A worker may not invent a new one.
PERMITTED_OUTPUT_SCHEMAS: tuple[str, ...] = ("efah.work_unit_candidate",)


def _resolve_work_unit(services: WorkflowServices, state: WorkflowState) -> WorkUnit:
    spec = state.get("artifacts", {}).get("work_unit_spec")
    if isinstance(spec, dict):
        return WorkUnit(**spec)
    wanted = state.get("work_unit_id")
    for unit in decompose(services.pack):
        if unit.work_unit_id == wanted:
            return unit
    raise ClassifiedFailure(
        FailureClass.WIRING_FAILURE,
        f"work unit {wanted!r} is not present in the compiled plan; no path to execute it",
    )


def build_task_graph(services: WorkflowServices) -> StateGraph:
    """START -> claim_lease -> execute -> submit_candidate -> record_outcome -> END."""
    builder = StateGraph(WorkflowState)
    oracle = LeaseFencingOracle(services.ledger)

    @node(TASK_GRAPH_ID, "claim_lease", services)
    def claim_lease(state: WorkflowState) -> dict[str, Any]:
        unit = _resolve_work_unit(services, state)
        try:
            lease = services.ledger.acquire(
                work_unit_id=unit.work_unit_id,
                role=services.default_role,
                blinded_alias=services.default_blinded_alias,
                branch=f"feat/{unit.work_unit_id.lower()}",
                worktree=f"{services.worktree_root}/{unit.work_unit_id.lower()}",
                input_hashes={"work_unit": unit.input_hash},
                permitted_output_schemas=PERMITTED_OUTPUT_SCHEMAS,
                ownership_mode=OwnershipMode.EXCLUSIVE,
            )
        except LeaseError as exc:
            # Section 9.3: a contended work unit is BLOCKED_DEPENDENCY, not a
            # retryable infrastructure fault and never an owner interrupt.
            return {
                "typed_blockers": [str(TaskState.BLOCKED_DEPENDENCY)],
                "artifacts": {"lease_refusal": str(exc), "work_unit_spec": unit.model_dump(mode="json")},
            }
        return {
            "work_unit_id": unit.work_unit_id,
            "lease_generation": lease.generation,
            "input_hashes": {"work_unit": unit.input_hash},
            "artifacts": {
                "work_unit_spec": unit.model_dump(mode="json"),
                "lease": lease.model_dump(mode="json"),
            },
        }

    @node(TASK_GRAPH_ID, "execute_work_unit", services)
    def execute_work_unit(state: WorkflowState) -> dict[str, Any]:
        """Produce the candidate artifact for this work unit.

        Deterministic and content-addressed: the artifact hash is a function of
        the work unit and its inputs only, so a resumed run re-derives the same
        hash and the idempotency key stays stable (Section 10.6).
        """
        artifacts = state.get("artifacts", {})
        if "lease" not in artifacts:
            return {"artifacts": {"execution_skipped": "no lease"}}
        unit = WorkUnit(**artifacts["work_unit_spec"])
        candidate = {
            "schema_id": PERMITTED_OUTPUT_SCHEMAS[0],
            "work_unit_id": unit.work_unit_id,
            "requirement_ids": unit.requirement_ids,
            "gate_ids": unit.gate_ids,
            "contract_version": unit.contract_version,
            "success_conditions": unit.success_conditions,
        }
        digest = content_hash(candidate)
        return {
            "artifacts": {"candidate": candidate},
            "output_hashes": {f"candidate.{unit.work_unit_id}": digest},
            "pending_gates": list(unit.gate_ids),
            "gate_verdicts": {
                "idempotency_key": idempotency_key(
                    work_unit_id=unit.work_unit_id,
                    graph_id=TASK_GRAPH_ID,
                    node="execute_work_unit",
                    input_hashes=state.get("input_hashes", {}),
                )
            },
        }

    @node(TASK_GRAPH_ID, "submit_candidate", services)
    def submit_candidate(state: WorkflowState) -> dict[str, Any]:
        """ORACLE-002 runs before the candidate is applied anywhere."""
        artifacts = state.get("artifacts", {})
        lease = artifacts.get("lease")
        if lease is None:
            return {"gate_verdicts": {"ORACLE-002": {"verdict": str(Verdict.UNVERIFIABLE)}}}
        applied: list[str] = []
        gateway = SubmissionGateway(oracle, applier=lambda s: applied.append(s.work_unit_id))
        submission = Submission(
            work_unit_id=lease["work_unit_id"],
            lease_id=lease["lease_id"],
            lease_generation=lease["generation"],
            branch=lease["branch"],
            worktree=lease["worktree"],
            input_hashes=dict(lease["input_hashes"]),
            output_schema=PERMITTED_OUTPUT_SCHEMAS[0],
            artifact_hashes={
                k: v for k, v in state.get("output_hashes", {}).items() if k.startswith("candidate.")
            },
            submitted_by_alias=lease["blinded_alias"],
        )
        verdict = gateway.submit(submission)
        blockers = [str(TaskState.STALE_ASSIGNMENT)] if verdict.is_stale else []
        return {
            "gate_verdicts": {"ORACLE-002": verdict.model_dump(mode="json")},
            "typed_blockers": blockers,
            "artifacts": {"submission_applied": applied},
        }

    @node(TASK_GRAPH_ID, "record_outcome", services)
    def record_outcome(state: WorkflowState) -> dict[str, Any]:
        """Section 9.3: a worker's ceiling is CANDIDATE_COMPLETE."""
        verdicts = state.get("gate_verdicts", {})
        oracle_result = verdicts.get("ORACLE-002", {})
        verdict = oracle_result.get("verdict")
        blockers = list(state.get("typed_blockers", []))
        if str(TaskState.STALE_ASSIGNMENT) in blockers:
            task_state = TaskState.STALE_ASSIGNMENT
        elif str(TaskState.BLOCKED_DEPENDENCY) in blockers:
            task_state = TaskState.BLOCKED_DEPENDENCY
        elif verdict == str(Verdict.PASS):
            task_state = TaskState.CANDIDATE_COMPLETE
        else:
            task_state = TaskState.FAILED_PROVENANCE
        return {"artifacts": {"task_state": str(task_state)}}

    builder.add_node("claim_lease", claim_lease)
    builder.add_node("execute_work_unit", execute_work_unit)
    builder.add_node("submit_candidate", submit_candidate)
    builder.add_node("record_outcome", record_outcome)
    builder.add_edge(START, "claim_lease")
    builder.add_edge("claim_lease", "execute_work_unit")
    builder.add_edge("execute_work_unit", "submit_candidate")
    builder.add_edge("submit_candidate", "record_outcome")
    builder.add_edge("record_outcome", END)
    return builder


def build_build_graph(services: WorkflowServices) -> StateGraph:
    """START -> select_work_units -> run_task_graphs -> summarize -> END.

    Runs ``task_graph`` per selected work unit. The task graph is compiled
    without its own checkpointer here: from the parent's perspective a work unit
    is one durable step, and nesting checkpointers would let a resumed parent
    disagree with a resumed child about which step is next.
    """
    builder = StateGraph(WorkflowState)
    task_graph = build_task_graph(services).compile()

    @node(GRAPH_ID, "select_work_units", services)
    def select_work_units(state: WorkflowState) -> dict[str, Any]:
        units = list(state.get("work_units", []))
        if not units:
            units = [u.model_dump(mode="json") for u in decompose(services.pack)]
        selected = units[: services.max_work_units]
        return {
            "work_units": units,
            "artifacts": {"selected_work_units": [u["work_unit_id"] for u in selected]},
        }

    @node(GRAPH_ID, "run_task_graphs", services)
    def run_task_graphs(state: WorkflowState) -> dict[str, Any]:
        selected = state.get("artifacts", {}).get("selected_work_units", [])
        by_id = {u["work_unit_id"]: u for u in state.get("work_units", [])}
        outcomes: dict[str, Any] = {}
        output_hashes: dict[str, str] = {}
        pending: list[str] = []
        log: list[str] = []
        blockers: list[str] = []
        generation = state.get("lease_generation", 0)

        for work_unit_id in selected:
            child: dict[str, Any] = {
                **{k: v for k, v in state.items() if k not in ("artifacts", "node_log", "gate_verdicts")},
                "work_unit_id": work_unit_id,
                "node_log": [],
                "gate_verdicts": {},
                "artifacts": {"work_unit_spec": by_id.get(work_unit_id)},
            }
            result = task_graph.invoke(child)
            outcomes[work_unit_id] = {
                "task_state": result.get("artifacts", {}).get("task_state"),
                "oracle_002": result.get("gate_verdicts", {}).get("ORACLE-002", {}).get("verdict"),
            }
            output_hashes.update(result.get("output_hashes", {}))
            pending.extend(result.get("pending_gates", []))
            blockers.extend(result.get("typed_blockers", []))
            log.extend(result.get("node_log", []))
            generation = max(generation, result.get("lease_generation", 0))

        return {
            "artifacts": {"work_unit_outcomes": outcomes},
            "output_hashes": output_hashes,
            "pending_gates": pending,
            "typed_blockers": blockers,
            "node_log": log,
            "lease_generation": generation,
        }

    @node(GRAPH_ID, "summarize_build", services)
    def summarize_build(state: WorkflowState) -> dict[str, Any]:
        outcomes = state.get("artifacts", {}).get("work_unit_outcomes", {})
        candidates = [k for k, v in outcomes.items() if v.get("task_state") == str(TaskState.CANDIDATE_COMPLETE)]
        return {
            "artifacts": {"candidate_work_units": sorted(candidates)},
            "output_hashes": {"build.summary": content_hash(outcomes)},
        }

    builder.add_node("select_work_units", select_work_units)
    builder.add_node("run_task_graphs", run_task_graphs)
    builder.add_node("summarize_build", summarize_build)
    builder.add_edge(START, "select_work_units")
    builder.add_edge("select_work_units", "run_task_graphs")
    builder.add_edge("run_task_graphs", "summarize_build")
    builder.add_edge("summarize_build", END)
    return builder
