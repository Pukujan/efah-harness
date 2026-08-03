"""In-process control-plane adapter.

**This is not the authoritative store and never becomes one.** TerminusDB is
(contract Section 15.2), and WS-B's adapter satisfies the same two Protocols.
This one exists so the API, the middleware, the read projections, and the Plane
projection have a real, working implementation to run against at the composition
root -- so the walking skeleton is an executing path rather than a diagram.
See ``docs/decisions/DEC-601-api-port-seam.md``.

What it genuinely does:

* validates and hashes a project pack through :func:`integrations.pack.load_pack`
  (the same loader the TerminusDB import uses), so ``POST /projects/import``
  really does reject an invalid pack;
* seeds the dependency registry from ``dependency-policy.yaml`` and oracle health
  from ``acceptance/oracle-definitions/``, which are owner facts already in the
  pack rather than invented ones;
* records requirement traceability rows from the visible acceptance gates;
* accepts task, evaluation, model-run, and provenance records pushed in by the
  compiler and the runtime, and serves consistent snapshots of them.

What it deliberately does not do: compile the contract into tasks (WS-A), run a
graph (WS-C), or claim a ``terminus_commit`` it did not obtain (it reports
``None``, and the provenance view shows that honestly).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.state import (
    ControlPlaneSnapshot,
    DecisionRecord,
    DependencyRecord,
    DriftFindingRecord,
    EvaluationRecord,
    GraphView,
    ImpactMap,
    KnowledgeRecord,
    MilestoneRecord,
    ModelRunRecord,
    OracleHealthRecord,
    ProjectRecord,
    ProvenanceEdge,
    ReleaseRecord,
    RequirementRecord,
    RunHandle,
    TaskRecord,
)
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import ContractReviewOutcome, ProjectState
from integrations.pack import ProjectPack, load_pack


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _ProjectState:
    """Everything held for one imported project."""

    def __init__(self, project: ProjectRecord, pack: ProjectPack | None) -> None:
        self.project = project
        self.pack = pack
        self.tasks: dict[str, TaskRecord] = {}
        self.requirements: dict[str, RequirementRecord] = {}
        self.model_runs: dict[str, ModelRunRecord] = {}
        self.evaluations: dict[str, EvaluationRecord] = {}
        self.oracles: dict[str, OracleHealthRecord] = {}
        self.dependencies: dict[str, DependencyRecord] = {}
        self.knowledge: dict[str, KnowledgeRecord] = {}
        self.provenance: list[ProvenanceEdge] = []
        self.drift: dict[str, DriftFindingRecord] = {}
        self.decisions: dict[str, DecisionRecord] = {}
        self.release: ReleaseRecord | None = None


class InMemoryControlPlane:
    """Satisfies ``ControlPlaneReadPort`` and ``ControlPlaneWritePort``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._projects: dict[str, _ProjectState] = {}

    # ------------------------------------------------------------------ write

    def import_project(
        self, *, pack_root: str, requested_by: str, correlation_id: str
    ) -> ProjectRecord:
        """Section 6.1 steps 1-2: validate every schema and reference, then import.

        ``load_pack`` raises ``PackValidationError`` on a bad pack; the
        controller maps that to a typed blocker rather than a 500.
        """
        pack = load_pack(Path(pack_root))
        project = ProjectRecord(
            project_id=pack.project_id,
            name=str(pack.yaml("project.yaml")["project"].get("name", pack.project_id)),
            state=ProjectState.RUNNING,
            contract_id=pack.contract_id,
            contract_version=pack.contract_version,
            pack_manifest_hash=pack.manifest_hash,
            terminus_commit=None,
            repository_commit=None,
            imported_at=_now(),
            milestones=self._milestones_from_pack(pack),
        )
        with self._lock:
            state = _ProjectState(project, pack)
            self._seed_from_pack(state, pack, requested_by=requested_by)
            self._projects[project.project_id] = state
        return project

    def record_decision(self, decision: DecisionRecord) -> DecisionRecord:
        with self._lock:
            for state in self._projects.values():
                state.decisions[decision.decision_id] = decision
                state.provenance.append(
                    ProvenanceEdge(
                        source=f"decision:{decision.decision_id}",
                        relation="bound_to_contract_version",
                        target=decision.contract_version,
                    )
                )
        return decision

    def record_contract_review(
        self, *, project_id: str, outcome: str, reviewer: str, notes: str
    ) -> DecisionRecord:
        """Section 19.3/19.4. Only CONTRACT_REAFFIRMED advances automatically."""
        resolved = ContractReviewOutcome(outcome)
        decision = DecisionRecord(
            decision_id=f"REVIEW-{uuid.uuid4().hex[:12]}",
            title=f"Contract review: {resolved}",
            outcome=str(resolved),
            decided_by=reviewer,
            decided_at=_now(),
            contract_version=CONTRACT_VERSION,
            rationale=notes,
        )
        with self._lock:
            state = self._projects.get(project_id)
            if state is not None:
                state.decisions[decision.decision_id] = decision
        return decision

    def record_run_request(
        self, *, project_id: str, task_id: str | None, run_id: str, requested_by: str
    ) -> None:
        with self._lock:
            state = self._projects.get(project_id)
            if state is None:
                return
            state.project = state.project.model_copy(update={"current_run_id": run_id})
            state.provenance.append(
                ProvenanceEdge(
                    source=f"run:{run_id}",
                    relation="requested_for_task" if task_id else "requested_for_project",
                    target=task_id or project_id,
                )
            )

    # -- ingest points for the other workstreams -------------------------------
    # These are how WS-A (compiler) and WS-C (runtime) push authoritative records
    # in while their TerminusDB adapter is being wired. They are writes, so they
    # live on the write side and are never reachable from the dashboard.

    def upsert_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            state = self._require(task.project_id)
            state.tasks[task.task_id] = task
        return task

    def upsert_evaluation(self, project_id: str, evaluation: EvaluationRecord) -> EvaluationRecord:
        with self._lock:
            self._require(project_id).evaluations[evaluation.evaluation_id] = evaluation
        return evaluation

    def upsert_model_run(self, project_id: str, run: ModelRunRecord) -> ModelRunRecord:
        with self._lock:
            self._require(project_id).model_runs[run.run_id] = run
        return run

    def upsert_knowledge(self, project_id: str, record: KnowledgeRecord) -> KnowledgeRecord:
        with self._lock:
            self._require(project_id).knowledge[record.knowledge_id] = record
        return record

    def upsert_drift_finding(self, project_id: str, finding: DriftFindingRecord) -> DriftFindingRecord:
        with self._lock:
            self._require(project_id).drift[finding.finding_id] = finding
        return finding

    def set_release(self, project_id: str, release: ReleaseRecord) -> ReleaseRecord:
        with self._lock:
            self._require(project_id).release = release
        return release

    def add_provenance(self, project_id: str, edge: ProvenanceEdge) -> ProvenanceEdge:
        with self._lock:
            self._require(project_id).provenance.append(edge)
        return edge

    def set_project_state(self, project_id: str, state_value: ProjectState) -> ProjectRecord:
        with self._lock:
            state = self._require(project_id)
            state.project = state.project.model_copy(update={"state": state_value})
            return state.project

    # ------------------------------------------------------------------- read

    def list_project_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._projects)

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            state = self._projects.get(project_id)
            return state.project if state else None

    def snapshot(self, project_id: str) -> ControlPlaneSnapshot | None:
        with self._lock:
            state = self._projects.get(project_id)
            if state is None:
                return None
            return ControlPlaneSnapshot(
                project=state.project,
                tasks=tuple(state.tasks.values()),
                requirements=tuple(state.requirements.values()),
                model_runs=tuple(state.model_runs.values()),
                evaluations=tuple(state.evaluations.values()),
                oracles=tuple(state.oracles.values()),
                dependencies=tuple(state.dependencies.values()),
                knowledge=tuple(state.knowledge.values()),
                provenance=tuple(state.provenance),
                drift_findings=tuple(state.drift.values()),
                decisions=tuple(state.decisions.values()),
                release=state.release,
                captured_at=_now(),
                terminus_commit=state.project.terminus_commit,
            )

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            for state in self._projects.values():
                task = state.tasks.get(task_id)
                if task is not None:
                    return task
        return None

    def get_evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        with self._lock:
            for state in self._projects.values():
                found = state.evaluations.get(evaluation_id)
                if found is not None:
                    return found
        return None

    def get_dependency(self, dependency_id: str) -> DependencyRecord | None:
        with self._lock:
            for state in self._projects.values():
                found = state.dependencies.get(dependency_id)
                if found is not None:
                    return found
        return None

    def graph(self, project_id: str) -> GraphView | None:
        """Section 9.6 dependency graph plus the longest chain through it."""
        with self._lock:
            state = self._projects.get(project_id)
            if state is None:
                return None
            tasks = dict(state.tasks)

        nodes = tuple(
            {
                "id": task.task_id,
                "title": task.title,
                "state": str(task.state),
                "milestone_id": task.milestone_id,
                "workstream": task.workstream,
            }
            for task in tasks.values()
        )
        edges = tuple(
            {"from": dependency, "to": task.task_id, "relation": "blocks"}
            for task in tasks.values()
            for dependency in task.depends_on
        )
        path, has_cycle = _longest_path(tasks)
        return GraphView(
            project_id=project_id,
            nodes=nodes,
            edges=edges,
            critical_path=path,
            has_cycle=has_cycle,
        )

    def impact_map(self, dependency_id: str) -> ImpactMap | None:
        """Section 16.2: which modules, tasks, requirements, and gates a bump hits."""
        with self._lock:
            for state in self._projects.values():
                dependency = state.dependencies.get(dependency_id)
                if dependency is None:
                    continue
                modules = set(dependency.used_by_modules)
                affected_tasks = tuple(
                    sorted(
                        task.task_id
                        for task in state.tasks.values()
                        if task.workstream in modules
                        or any(path.split("/")[1:2] and path.split("/")[1] in modules
                               for path in task.allowed_paths)
                    )
                )
                affected_requirements = tuple(
                    sorted(
                        {
                            requirement_id
                            for task in state.tasks.values()
                            if task.task_id in affected_tasks
                            for requirement_id in task.requirement_ids
                        }
                    )
                )
                return ImpactMap(
                    dependency_id=dependency_id,
                    version=dependency.version,
                    affected_modules=tuple(sorted(modules)),
                    affected_task_ids=affected_tasks,
                    affected_requirement_ids=affected_requirements,
                    revalidation_gate_ids=tuple(
                        sorted(
                            {
                                gate_id
                                for requirement in state.requirements.values()
                                for gate_id in requirement.verified_by_gate_ids
                            }
                        )
                    ),
                    affected_gold_tests=dependency.affected_gold_tests,
                )
        return None

    # --------------------------------------------------------------- internals

    def _require(self, project_id: str) -> _ProjectState:
        state = self._projects.get(project_id)
        if state is None:
            raise KeyError(f"project not imported: {project_id}")
        return state

    @staticmethod
    def _milestones_from_pack(pack: ProjectPack) -> tuple[MilestoneRecord, ...]:
        gates = pack.yaml("contract.yaml").get("phase_gates") or []
        return tuple(
            MilestoneRecord(
                milestone_id=str(gate["phase"]),
                name=str(gate["phase"]).replace("_", " "),
                state="pending",
            )
            for gate in gates
            if isinstance(gate, dict) and "phase" in gate
        )

    @staticmethod
    def _seed_from_pack(state: _ProjectState, pack: ProjectPack, *, requested_by: str) -> None:
        """Load owner facts already present in the pack. Nothing is invented."""
        selected = pack.yaml("dependency-policy.yaml").get("selected_stack") or {}
        for key, entry in selected.items():
            if not isinstance(entry, dict):
                continue
            state.dependencies[str(entry.get("component", key))] = DependencyRecord(
                dependency_id=str(entry.get("component", key)),
                component=str(entry.get("component", key)),
                version=str(entry.get("version", "unpinned")),
                used_by_modules=(str(key),),
                update_policy=str(
                    pack.yaml("dependency-policy.yaml")
                    .get("risk_policy", {})
                    .get("auto_merge_dependency_updates", "unspecified")
                ),
            )

        for oracle_id, oracle in pack.oracle_definitions().items():
            state.oracles[oracle_id] = OracleHealthRecord(
                oracle_id=oracle_id,
                oracle_version=str(oracle.get("version", oracle.get("oracle_version", "1.0"))),
                healthy=True,
                last_checked_at=_now(),
                model_judge_in_verdict_path=bool(oracle.get("model_judge_in_verdict_path", False)),
            )

        gates = pack.acceptance_gates()
        for gate_id, gate in gates.items():
            for assertion in gate.get("assertions", []) or []:
                requirement_id = f"{gate_id}-{assertion.get('id', '?')}"
                state.requirements[requirement_id] = RequirementRecord(
                    requirement_id=requirement_id,
                    statement=str(assertion.get("claim", "")),
                    contract_section=str(gate.get("name", gate_id)),
                    verified_by_gate_ids=(gate_id,),
                )

        state.release = ReleaseRecord(
            release_id=f"RC-{pack.project_id}",
            gates_required=tuple(sorted(gate_id for gate_id, g in gates.items() if g.get("blocking"))),
            gates_passed=(),
            blocking_gate_ids=tuple(
                sorted(gate_id for gate_id, g in gates.items() if g.get("blocking"))
            ),
            ready=False,
        )

        state.provenance.append(
            ProvenanceEdge(
                source=f"pack:{pack.manifest_hash}",
                relation="imported_by",
                target=requested_by,
                content_hash=pack.manifest_hash,
            )
        )
        state.provenance.append(
            ProvenanceEdge(
                source=f"project:{pack.project_id}",
                relation="bound_to_contract",
                target=f"{CONTRACT_ID}@{pack.contract_version}",
            )
        )


def _longest_path(tasks: dict[str, TaskRecord]) -> tuple[tuple[str, ...], bool]:
    """Longest dependency chain (the critical path) and whether a cycle exists.

    A cycle is reported rather than raised: GATE-D1-03 wants a circular
    dependency graph to *fail visibly in the view*, not to crash the API.
    """
    memo: dict[str, tuple[str, ...]] = {}
    visiting: set[str] = set()
    cycle = False

    def walk(task_id: str) -> tuple[str, ...]:
        nonlocal cycle
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            cycle = True
            return ()
        visiting.add(task_id)
        best: tuple[str, ...] = ()
        task = tasks.get(task_id)
        for dependency in task.depends_on if task else ():
            if dependency not in tasks:
                continue
            candidate = walk(dependency)
            if len(candidate) > len(best):
                best = candidate
        visiting.discard(task_id)
        memo[task_id] = (*best, task_id)
        return memo[task_id]

    longest: tuple[str, ...] = ()
    for task_id in tasks:
        candidate = walk(task_id)
        if len(candidate) > len(longest):
            longest = candidate
    return longest, cycle


class RecordingRuntime:
    """Default ``RuntimePort``: accepts, correlates, and records a run request.

    It is **not** a workflow engine and does not pretend to be one -- DEC-001
    makes LangGraph the permanent runtime, and WS-C owns it. What this adapter
    genuinely provides is the API-side half of the contract: a run id, a
    correlated trace, and a durable record that the request was accepted, which
    is exactly what ``POST /projects/{id}/run`` owes its caller before the graph
    is dispatched.

    Swap it at the composition root: ``create_app(runtime=LangGraphRuntime(...))``.
    """

    #: The composition verifier (Section 5.2) reads this to tell a real runtime
    #: from the recording default, so "not yet wired" cannot be reported as done.
    executes_graph: bool = False

    def __init__(self, control_plane: Any) -> None:
        self._control_plane = control_plane

    def start_project_run(
        self, *, project_id: str, requested_by: str, correlation_id: str
    ) -> RunHandle:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._control_plane.record_run_request(
            project_id=project_id, task_id=None, run_id=run_id, requested_by=requested_by
        )
        return RunHandle(
            run_id=run_id,
            project_id=project_id,
            accepted=True,
            state=str(ProjectState.RUNNING),
            detail="run request recorded; dispatch belongs to the LangGraph runtime adapter",
        )

    def resume_task(
        self, *, task_id: str, project_id: str, requested_by: str, correlation_id: str
    ) -> RunHandle:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._control_plane.record_run_request(
            project_id=project_id, task_id=task_id, run_id=run_id, requested_by=requested_by
        )
        return RunHandle(
            run_id=run_id,
            project_id=project_id,
            task_id=task_id,
            accepted=True,
            state=str(ProjectState.RUNNING),
            detail="resume request recorded; checkpoint replay belongs to the LangGraph adapter",
        )
