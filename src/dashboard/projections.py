"""Build the thirteen read projections (contract Sections 5.1, 11.6).

Read-only by construction, three ways over:

1. the input is a frozen :class:`api.state.ControlPlaneSnapshot`;
2. the only control-plane handle this module will accept is a
   :class:`dashboard.source.ReadOnlySource`, whose write half does not exist;
3. every output model is frozen.

Section 9.8 is honoured here rather than trusted: durations are *derived* from
the system-event timestamps on each record. There is no code path that reads an
estimate, because :class:`api.state.TimingRecord` has no estimate field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from api.state import ControlPlaneSnapshot, TimingRecord
from dashboard.redaction import assert_no_protected_content
from dashboard.source import ReadOnlySource
from dashboard.views import (
    BlockerAndOwnerDecision,
    ContractAndRequirementTraceability,
    DashboardProjection,
    DependencyRow,
    DependencyVersionsAndImpactMaps,
    DriftRow,
    EvaluationStatusRow,
    KnowledgeAndHardGoldPromotion,
    KnowledgeRow,
    LedgerRow,
    MilestoneStatus,
    ModelRunAliasesAndRoleHistory,
    ModelRunRow,
    OracleHealthAndMutantResults,
    OracleRow,
    OwnershipRow,
    ProjectAndMilestoneStatus,
    ProvenanceEdgeView,
    ProvenanceGraph,
    ReleaseReadiness,
    ScopeDriftFindings,
    TaskLedgerAndCriticalPath,
    TaskOwnershipAndLeases,
    TraceabilityRow,
    TypedBlocker,
    VisibleAndHiddenEvaluationStatus,
)
from governance.envelope import TRUSTED_KNOWLEDGE_FLOOR, KnowledgeTier
from governance.states import TERMINAL_PROJECT_STATES, OwnerInterrupt, TaskState

#: Contract Section 12.1/12.2: these roles produce evidence a gate relies on, so
#: DEC-002 routes them through the eval gateway. Seeing one on production is a
#: provenance failure, and the dashboard is where the owner would notice.
GATE_BEARING_ROLES = frozenset(
    {
        "sealed_holdout_author",
        "mutant_author",
        "oracle_author",
        "judge",
        "auditor",
        "compliance",
        "release_verifier",
        "adversarial_critic",
        "visible_test_author",
    }
)

#: Section 9.3 states in which a task is genuinely occupying a worker.
_ACTIVE_TASK_STATES = frozenset(
    {TaskState.CLAIMED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.CANDIDATE_COMPLETE}
)

_BLOCKED_TASK_STATES = frozenset(
    {
        TaskState.BLOCKED_DEPENDENCY,
        TaskState.BLOCKED_OWNER_DECISION,
        TaskState.BLOCKED_EXTERNAL_ACCESS,
    }
)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _span(start: str | None, end: str | None) -> float | None:
    """Seconds between two system events, or ``None`` when either is missing.

    ``None`` -- never ``0.0`` -- when unknown: a zero would read as "no time
    spent", and a projection that guesses is a projection that lies.
    """
    first, second = _parse(start), _parse(end)
    if first is None or second is None:
        return None
    return max((second - first).total_seconds(), 0.0)


def derived_durations(timing: TimingRecord) -> dict[str, float | None]:
    """Section 9.8 derived durations. System events only, no agent estimates."""
    return {
        "queue_duration": _span(timing.queued_at, timing.claimed_at),
        "active_duration": _span(timing.started_at, timing.candidate_submitted_at)
        or _span(timing.started_at, timing.completed_at),
        "blocked_duration": _span(timing.blocked_at, timing.resumed_at),
        "evaluation_duration": _span(timing.verification_started_at, timing.completed_at),
        "human_wait_duration": _span(timing.blocked_at, timing.resumed_at),
        "rework_duration": _span(timing.resumed_at, timing.candidate_submitted_at),
        "total_wall_clock": _span(timing.queued_at, timing.merged_at)
        or _span(timing.queued_at, timing.completed_at),
    }


# --------------------------------------------------------------------------- 1
def project_and_milestone_status(snapshot: ControlPlaneSnapshot) -> ProjectAndMilestoneStatus:
    counts: dict[str, int] = {}
    for task in snapshot.tasks:
        counts[str(task.state)] = counts.get(str(task.state), 0) + 1

    milestone_totals: dict[str, tuple[int, int]] = {}
    for task in snapshot.tasks:
        if task.milestone_id is None:
            continue
        done, total = milestone_totals.get(task.milestone_id, (0, 0))
        finished = task.state in {TaskState.PASSED, TaskState.MERGED, TaskState.CLOSED}
        milestone_totals[task.milestone_id] = (done + int(finished), total + 1)

    milestones = tuple(
        MilestoneStatus(
            milestone_id=milestone.milestone_id,
            name=milestone.name,
            state=milestone.state,
            target_date=milestone.target_date,
            completed_tasks=milestone_totals.get(milestone.milestone_id, (0, 0))[0],
            total_tasks=milestone_totals.get(milestone.milestone_id, (0, 0))[1],
        )
        for milestone in snapshot.project.milestones
    )
    return ProjectAndMilestoneStatus(
        project_id=snapshot.project.project_id,
        name=snapshot.project.name,
        state=snapshot.project.state,
        contract_id=snapshot.project.contract_id,
        contract_version=snapshot.project.contract_version,
        is_terminal=snapshot.project.state in TERMINAL_PROJECT_STATES,
        current_run_id=snapshot.project.current_run_id,
        imported_at=snapshot.project.imported_at,
        pack_manifest_hash=snapshot.project.pack_manifest_hash,
        milestones=milestones,
        task_state_counts=counts,
    )


# --------------------------------------------------------------------------- 2
def task_ledger_and_critical_path(
    snapshot: ControlPlaneSnapshot, *, critical_path: tuple[str, ...] = (), has_cycle: bool = False
) -> TaskLedgerAndCriticalPath:
    on_path = set(critical_path)
    rows = []
    for task in sorted(snapshot.tasks, key=lambda t: t.task_id):
        durations = derived_durations(task.timing)
        rows.append(
            LedgerRow(
                task_id=task.task_id,
                title=task.title,
                state=task.state,
                milestone_id=task.milestone_id,
                workstream=task.workstream,
                depends_on=task.depends_on,
                on_critical_path=task.task_id in on_path or task.on_critical_path,
                active_duration_seconds=durations["active_duration"],
                blocked_duration_seconds=durations["blocked_duration"],
                total_wall_clock_seconds=durations["total_wall_clock"],
            )
        )
    # Section 19.2 UNLINKED_TASK: work that traces to no requirement.
    unlinked = tuple(sorted(t.task_id for t in snapshot.tasks if not t.requirement_ids))
    return TaskLedgerAndCriticalPath(
        rows=tuple(rows),
        critical_path=critical_path,
        has_cycle=has_cycle,
        unlinked_task_ids=unlinked,
    )


# --------------------------------------------------------------------------- 3
def task_ownership(snapshot: ControlPlaneSnapshot) -> TaskOwnershipAndLeases:
    rows = []
    stale: list[str] = []
    unowned: list[str] = []
    for task in sorted(snapshot.tasks, key=lambda t: t.task_id):
        lease = task.lease
        if lease is None:
            if task.state in _ACTIVE_TASK_STATES:
                unowned.append(task.task_id)
            continue
        rows.append(
            OwnershipRow(
                task_id=task.task_id,
                holder_alias=lease.holder_alias,
                worktree=lease.worktree,
                fence_token=lease.fence_token,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
                last_heartbeat_at=lease.last_heartbeat_at,
                is_stale=lease.is_stale,
            )
        )
        if lease.is_stale:
            stale.append(task.task_id)
    return TaskOwnershipAndLeases(
        rows=tuple(rows),
        stale_session_task_ids=tuple(stale),
        unowned_running_task_ids=tuple(unowned),
    )


# --------------------------------------------------------------------------- 4
def traceability(snapshot: ControlPlaneSnapshot) -> ContractAndRequirementTraceability:
    by_requirement: dict[str, list[str]] = {}
    for task in snapshot.tasks:
        for requirement_id in task.requirement_ids:
            by_requirement.setdefault(requirement_id, []).append(task.task_id)

    rows = []
    untraced: list[str] = []
    for requirement in sorted(snapshot.requirements, key=lambda r: r.requirement_id):
        satisfied = tuple(
            sorted(set(requirement.satisfied_by_task_ids) | set(by_requirement.get(requirement.requirement_id, [])))
        )
        traced = bool(satisfied) and bool(requirement.verified_by_gate_ids)
        if not traced:
            untraced.append(requirement.requirement_id)
        rows.append(
            TraceabilityRow(
                requirement_id=requirement.requirement_id,
                statement=requirement.statement,
                contract_section=requirement.contract_section,
                satisfied_by_task_ids=satisfied,
                verified_by_gate_ids=requirement.verified_by_gate_ids,
                is_traced=traced,
            )
        )
    coverage = (len(rows) - len(untraced)) / len(rows) if rows else 0.0
    return ContractAndRequirementTraceability(
        contract_id=snapshot.project.contract_id,
        contract_version=snapshot.project.contract_version,
        rows=tuple(rows),
        untraced_requirement_ids=tuple(untraced),
        coverage_ratio=round(coverage, 4),
    )


# --------------------------------------------------------------------------- 5
def scope_drift(snapshot: ControlPlaneSnapshot) -> ScopeDriftFindings:
    rows = tuple(
        DriftRow(
            finding_id=finding.finding_id,
            finding_type=finding.finding_type,
            detail=finding.detail,
            subject=finding.subject,
            detected_at=finding.detected_at,
            resolved=finding.resolved,
        )
        for finding in sorted(snapshot.drift_findings, key=lambda f: f.finding_id)
    )
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[str(row.finding_type)] = by_type.get(str(row.finding_type), 0) + 1
    return ScopeDriftFindings(
        rows=rows,
        open_count=sum(1 for row in rows if not row.resolved),
        by_type=by_type,
    )


# --------------------------------------------------------------------------- 6
def model_runs(snapshot: ControlPlaneSnapshot) -> ModelRunAliasesAndRoleHistory:
    rows = tuple(
        ModelRunRow(
            run_id=run.run_id,
            model_alias=run.model_alias,
            role=run.role,
            gateway_class=run.gateway_class,
            task_id=run.task_id,
            started_at=run.started_at,
            duration_ms=run.duration_ms,
            outcome=run.outcome,
        )
        for run in sorted(snapshot.model_runs, key=lambda r: r.run_id)
    )
    misrouted = tuple(
        row.run_id
        for row in rows
        if row.role in GATE_BEARING_ROLES and row.gateway_class != "eval"
    )
    return ModelRunAliasesAndRoleHistory(
        rows=rows,
        aliases_seen=tuple(sorted({row.model_alias for row in rows})),
        roles_seen=tuple(sorted({row.role for row in rows})),
        gate_bearing_runs_on_production_gateway=misrouted,
    )


# --------------------------------------------------------------------------- 7
def evaluation_status(snapshot: ControlPlaneSnapshot) -> VisibleAndHiddenEvaluationStatus:
    rows = tuple(
        EvaluationStatusRow(
            evaluation_id=evaluation.evaluation_id,
            task_id=evaluation.task_id,
            visible_verdict=evaluation.visible_verdict,
            visible_passed=evaluation.visible_passed,
            visible_total=evaluation.visible_total,
            hidden_suite_name=evaluation.hidden_suite_name,
            hidden_suite_verdict=evaluation.hidden_suite_verdict,
            hidden_assertions_total=evaluation.hidden_assertions_total,
            hidden_assertions_failed=evaluation.hidden_assertions_failed,
            oracle_version=evaluation.oracle_version,
            failure_class=evaluation.failure_class,
        )
        for evaluation in sorted(snapshot.evaluations, key=lambda e: e.evaluation_id)
    )
    visible_total = sum(row.visible_total for row in rows)
    visible_passed = sum(row.visible_passed for row in rows)
    hidden_total = sum(row.hidden_assertions_total for row in rows)
    hidden_failed = sum(row.hidden_assertions_failed for row in rows)
    return VisibleAndHiddenEvaluationStatus(
        rows=rows,
        # Structurally false: no field on EvaluationStatusRow can carry content.
        protected_content_exposed=False,
        visible_pass_rate=round(visible_passed / visible_total, 4) if visible_total else None,
        hidden_pass_rate=(
            round((hidden_total - hidden_failed) / hidden_total, 4) if hidden_total else None
        ),
    )


# --------------------------------------------------------------------------- 8
def oracle_health(snapshot: ControlPlaneSnapshot) -> OracleHealthAndMutantResults:
    rows = tuple(
        OracleRow(
            oracle_id=oracle.oracle_id,
            oracle_version=oracle.oracle_version,
            healthy=oracle.healthy,
            last_checked_at=oracle.last_checked_at,
            mutants_total=oracle.mutants_total,
            mutants_killed=oracle.mutants_killed,
            mutants_survived=oracle.mutants_survived,
            kill_rate=(
                round(oracle.mutants_killed / oracle.mutants_total, 4)
                if oracle.mutants_total
                else None
            ),
            model_judge_in_verdict_path=oracle.model_judge_in_verdict_path,
        )
        for oracle in sorted(snapshot.oracles, key=lambda o: o.oracle_id)
    )
    return OracleHealthAndMutantResults(
        rows=rows,
        unhealthy_oracle_ids=tuple(row.oracle_id for row in rows if not row.healthy),
        oracles_with_judge_in_verdict_path=tuple(
            row.oracle_id for row in rows if row.model_judge_in_verdict_path
        ),
        surviving_mutants_total=sum(row.mutants_survived for row in rows),
    )


# --------------------------------------------------------------------------- 9
def dependencies(
    snapshot: ControlPlaneSnapshot, *, impact_maps: dict[str, dict[str, Any]] | None = None
) -> DependencyVersionsAndImpactMaps:
    rows = tuple(
        DependencyRow(
            dependency_id=dependency.dependency_id,
            component=dependency.component,
            version=dependency.version,
            pinned=_is_pinned(dependency.version),
            documentation_snapshot_id=dependency.documentation_snapshot_id,
            used_by_modules=dependency.used_by_modules,
            last_verified_run=dependency.last_verified_run,
        )
        for dependency in sorted(snapshot.dependencies, key=lambda d: d.dependency_id)
    )
    return DependencyVersionsAndImpactMaps(
        rows=rows,
        unpinned_dependency_ids=tuple(row.dependency_id for row in rows if not row.pinned),
        missing_snapshot_dependency_ids=tuple(
            row.dependency_id for row in rows if not row.documentation_snapshot_id
        ),
        impact_maps=impact_maps or {},
    )


def _is_pinned(version: str) -> bool:
    """``TODO_builder_probe`` and ``unpinned`` are not versions."""
    lowered = version.strip().lower()
    return bool(lowered) and not lowered.startswith("todo") and lowered not in {"unpinned", "n/a", "latest", "*"}


# -------------------------------------------------------------------------- 10
def knowledge(snapshot: ControlPlaneSnapshot) -> KnowledgeAndHardGoldPromotion:
    order = list(KnowledgeTier)
    floor_index = order.index(TRUSTED_KNOWLEDGE_FLOOR)
    rows = []
    below: list[str] = []
    for record in sorted(snapshot.knowledge, key=lambda k: k.knowledge_id):
        try:
            trusted = order.index(KnowledgeTier(record.tier)) >= floor_index
        except ValueError:
            trusted = False
        if not trusted:
            below.append(record.knowledge_id)
        rows.append(
            KnowledgeRow(
                knowledge_id=record.knowledge_id,
                statement=record.statement,
                tier=record.tier,
                trusted=trusted,
                promoted_to_hard_gold=record.promoted_to_hard_gold,
                promoted_at=record.promoted_at,
                evidence_refs=record.evidence_refs,
            )
        )
    return KnowledgeAndHardGoldPromotion(
        rows=tuple(rows),
        hard_gold_count=sum(1 for row in rows if row.promoted_to_hard_gold),
        below_trust_floor_ids=tuple(below),
    )


# -------------------------------------------------------------------------- 11
def provenance(snapshot: ControlPlaneSnapshot) -> ProvenanceGraph:
    edges = tuple(
        ProvenanceEdgeView(
            source=edge.source,
            relation=edge.relation,
            target=edge.target,
            terminus_commit=edge.terminus_commit,
            repository_commit=edge.repository_commit,
            content_hash=edge.content_hash,
        )
        for edge in snapshot.provenance
    )
    nodes = {edge.source for edge in edges} | {edge.target for edge in edges}
    return ProvenanceGraph(
        edges=edges,
        node_count=len(nodes),
        edges_without_commit_binding=sum(
            1 for edge in edges if not (edge.terminus_commit or edge.repository_commit or edge.content_hash)
        ),
    )


# -------------------------------------------------------------------------- 12
def release_readiness(snapshot: ControlPlaneSnapshot) -> ReleaseReadiness:
    release = snapshot.release
    if release is None:
        return ReleaseReadiness(ready=False, blocking_reason="no release candidate recorded")
    outstanding = tuple(sorted(set(release.gates_required) - set(release.gates_passed)))
    reason = None
    if outstanding:
        reason = f"{len(outstanding)} blocking gate(s) outstanding"
    elif not release.candidate_commit:
        reason = "no candidate commit bound"
    return ReleaseReadiness(
        release_id=release.release_id,
        candidate_commit=release.candidate_commit,
        ready=release.ready and not outstanding,
        gates_required=release.gates_required,
        gates_passed=release.gates_passed,
        gates_outstanding=outstanding,
        blocking_reason=reason,
    )


# -------------------------------------------------------------------------- 13
def typed_blockers(snapshot: ControlPlaneSnapshot) -> BlockerAndOwnerDecision:
    """Section 11.6 item 13 -- the *exact* typed blocker, not a summary."""
    blockers: list[TypedBlocker] = []
    for task in sorted(snapshot.tasks, key=lambda t: t.task_id):
        if task.state in _BLOCKED_TASK_STATES or task.owner_interrupt is not None:
            blockers.append(
                TypedBlocker(
                    subject=task.task_id,
                    blocker=str(task.state),
                    owner_interrupt=task.owner_interrupt,
                    detail=task.typed_blocker or "",
                )
            )
    requested = next((b for b in blockers if b.owner_interrupt is not None), None)
    if requested is None and snapshot.project.state.name.startswith("BLOCKED"):
        requested = TypedBlocker(
            subject=snapshot.project.project_id,
            blocker=str(snapshot.project.state),
            owner_interrupt=(
                OwnerInterrupt.OWNER_SCOPE_DECISION
                if snapshot.project.state.name == "BLOCKED_OWNER_DECISION"
                else None
            ),
            detail="project is in a blocked terminal state",
        )
        blockers.append(requested)
    return BlockerAndOwnerDecision(
        project_state=snapshot.project.state,
        blockers=tuple(blockers),
        requested_owner_decision=requested,
        awaiting_owner=requested is not None,
    )


# -----------------------------------------------------------------------------
def build_projection(
    snapshot: ControlPlaneSnapshot,
    *,
    critical_path: tuple[str, ...] = (),
    has_cycle: bool = False,
    impact_maps: dict[str, dict[str, Any]] | None = None,
) -> DashboardProjection:
    """Build all thirteen views, then refuse to return a leaking one."""
    projection = DashboardProjection(
        project_id=snapshot.project.project_id,
        contract_id=snapshot.project.contract_id,
        contract_version=snapshot.project.contract_version,
        captured_at=snapshot.captured_at,
        terminus_commit=snapshot.terminus_commit,
        project_and_milestone_status=project_and_milestone_status(snapshot),
        task_ledger_and_critical_path=task_ledger_and_critical_path(
            snapshot, critical_path=critical_path, has_cycle=has_cycle
        ),
        task_ownership_leases_worktrees_and_stale_sessions=task_ownership(snapshot),
        contract_and_requirement_traceability=traceability(snapshot),
        scope_drift_findings=scope_drift(snapshot),
        model_run_aliases_and_role_history=model_runs(snapshot),
        visible_and_hidden_evaluation_status=evaluation_status(snapshot),
        oracle_health_and_mutant_results=oracle_health(snapshot),
        dependency_versions_and_impact_maps=dependencies(snapshot, impact_maps=impact_maps),
        knowledge_and_hard_gold_promotion_state=knowledge(snapshot),
        provenance_graph=provenance(snapshot),
        release_readiness=release_readiness(snapshot),
        exact_typed_blocker_and_requested_owner_decision=typed_blockers(snapshot),
    )
    # The gate, applied to the rendered artifact rather than to its inputs.
    assert_no_protected_content(projection.model_dump(mode="json"), where="dashboard_projection")
    return projection


def project_from_source(source: ReadOnlySource, project_id: str) -> DashboardProjection | None:
    """Build the projection through the read-only handle.

    Refuses anything but a :class:`ReadOnlySource`. Accepting a bare control
    plane "for convenience" is exactly how Section 5.1 gets violated later.
    """
    if not isinstance(source, ReadOnlySource):
        raise TypeError(
            "the dashboard consumes read projections only (contract Section 5.1); "
            "wrap the control plane in dashboard.source.ReadOnlySource"
        )
    snapshot = source.snapshot(project_id)
    if snapshot is None:
        return None
    graph = source.graph(project_id)
    impact_maps: dict[str, dict[str, Any]] = {}
    for dependency in snapshot.dependencies:
        impact = source.impact_map(dependency.dependency_id)
        if impact is not None:
            impact_maps[dependency.dependency_id] = impact.model_dump(mode="json")
    return build_projection(
        snapshot,
        critical_path=graph.critical_path if graph else (),
        has_cycle=graph.has_cycle if graph else False,
        impact_maps=impact_maps,
    )
