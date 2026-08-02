"""The thirteen required dashboard views (contract Section 11.6).

One model per numbered view, in contract order, so a missing view is a missing
attribute rather than a missing paragraph in a document. ``DashboardProjection``
holds all thirteen and :data:`REQUIRED_VIEWS` names them; GATE-D2-11 A1's
"all thirteen present" check reads that tuple.

Every model is frozen. A read projection that can be edited after it is built is
a write path (Section 5.1).
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from governance.states import DriftFinding, OwnerInterrupt, ProjectState, TaskState, Verdict

#: plane.yaml -> required_views, in contract Section 11.6 order.
REQUIRED_VIEWS: Final = (
    "project_and_milestone_status",
    "task_ledger_and_critical_path",
    "task_ownership_leases_worktrees_and_stale_sessions",
    "contract_and_requirement_traceability",
    "scope_drift_findings",
    "model_run_aliases_and_role_history",
    "visible_and_hidden_evaluation_status",
    "oracle_health_and_mutant_results",
    "dependency_versions_and_impact_maps",
    "knowledge_and_hard_gold_promotion_state",
    "provenance_graph",
    "release_readiness",
    "exact_typed_blocker_and_requested_owner_decision",
)


class View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# 1 ---------------------------------------------------------------------------
class MilestoneStatus(View):
    milestone_id: str
    name: str
    state: str
    target_date: str | None = None
    completed_tasks: int = 0
    total_tasks: int = 0


class ProjectAndMilestoneStatus(View):
    project_id: str
    name: str
    state: ProjectState
    contract_id: str
    contract_version: str
    is_terminal: bool
    current_run_id: str | None = None
    imported_at: str | None = None
    pack_manifest_hash: str | None = None
    milestones: tuple[MilestoneStatus, ...] = ()
    task_state_counts: dict[str, int] = {}


# 2 ---------------------------------------------------------------------------
class LedgerRow(View):
    task_id: str
    title: str
    state: TaskState
    milestone_id: str | None = None
    workstream: str | None = None
    depends_on: tuple[str, ...] = ()
    on_critical_path: bool = False
    #: Section 9.8: derived from system-event timestamps only.
    active_duration_seconds: float | None = None
    blocked_duration_seconds: float | None = None
    total_wall_clock_seconds: float | None = None


class TaskLedgerAndCriticalPath(View):
    rows: tuple[LedgerRow, ...] = ()
    critical_path: tuple[str, ...] = ()
    has_cycle: bool = False
    unlinked_task_ids: tuple[str, ...] = ()


# 3 ---------------------------------------------------------------------------
class OwnershipRow(View):
    task_id: str
    holder_alias: str | None = None
    worktree: str | None = None
    fence_token: int = 0
    acquired_at: str | None = None
    expires_at: str | None = None
    last_heartbeat_at: str | None = None
    is_stale: bool = False


class TaskOwnershipAndLeases(View):
    rows: tuple[OwnershipRow, ...] = ()
    stale_session_task_ids: tuple[str, ...] = ()
    unowned_running_task_ids: tuple[str, ...] = ()


# 4 ---------------------------------------------------------------------------
class TraceabilityRow(View):
    requirement_id: str
    statement: str
    contract_section: str
    satisfied_by_task_ids: tuple[str, ...] = ()
    verified_by_gate_ids: tuple[str, ...] = ()
    is_traced: bool = False


class ContractAndRequirementTraceability(View):
    contract_id: str
    contract_version: str
    rows: tuple[TraceabilityRow, ...] = ()
    untraced_requirement_ids: tuple[str, ...] = ()
    coverage_ratio: float = 0.0


# 5 ---------------------------------------------------------------------------
class DriftRow(View):
    finding_id: str
    finding_type: DriftFinding
    detail: str
    subject: str | None = None
    detected_at: str | None = None
    resolved: bool = False


class ScopeDriftFindings(View):
    rows: tuple[DriftRow, ...] = ()
    open_count: int = 0
    by_type: dict[str, int] = {}


# 6 ---------------------------------------------------------------------------
class ModelRunRow(View):
    """Alias and role only. Section 11.2 -- no vendor field exists on this row."""

    run_id: str
    model_alias: str
    role: str
    gateway_class: str
    task_id: str | None = None
    started_at: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None


class ModelRunAliasesAndRoleHistory(View):
    rows: tuple[ModelRunRow, ...] = ()
    aliases_seen: tuple[str, ...] = ()
    roles_seen: tuple[str, ...] = ()
    #: DEC-002: a gate-bearing role on the production gateway is FAILED_PROVENANCE.
    gate_bearing_runs_on_production_gateway: tuple[str, ...] = ()


# 7 ---------------------------------------------------------------------------
class EvaluationStatusRow(View):
    """Status without content. There is no field here that could carry a holdout."""

    evaluation_id: str
    task_id: str | None = None
    visible_verdict: Verdict | None = None
    visible_passed: int = 0
    visible_total: int = 0
    hidden_suite_name: str | None = None
    hidden_suite_verdict: Verdict | None = None
    hidden_assertions_total: int = 0
    hidden_assertions_failed: int = 0
    oracle_version: str | None = None
    failure_class: str | None = None


class VisibleAndHiddenEvaluationStatus(View):
    rows: tuple[EvaluationStatusRow, ...] = ()
    protected_content_exposed: bool = False
    visible_pass_rate: float | None = None
    hidden_pass_rate: float | None = None


# 8 ---------------------------------------------------------------------------
class OracleRow(View):
    oracle_id: str
    oracle_version: str
    healthy: bool
    last_checked_at: str | None = None
    mutants_total: int = 0
    mutants_killed: int = 0
    mutants_survived: int = 0
    kill_rate: float | None = None
    model_judge_in_verdict_path: bool = False


class OracleHealthAndMutantResults(View):
    rows: tuple[OracleRow, ...] = ()
    unhealthy_oracle_ids: tuple[str, ...] = ()
    #: Section 17.3: a judge inside a deterministic verdict path invalidates it.
    oracles_with_judge_in_verdict_path: tuple[str, ...] = ()
    surviving_mutants_total: int = 0


# 9 ---------------------------------------------------------------------------
class DependencyRow(View):
    dependency_id: str
    component: str
    version: str
    pinned: bool
    documentation_snapshot_id: str | None = None
    used_by_modules: tuple[str, ...] = ()
    last_verified_run: str | None = None


class DependencyVersionsAndImpactMaps(View):
    rows: tuple[DependencyRow, ...] = ()
    unpinned_dependency_ids: tuple[str, ...] = ()
    missing_snapshot_dependency_ids: tuple[str, ...] = ()
    impact_maps: dict[str, dict[str, Any]] = {}


# 10 --------------------------------------------------------------------------
class KnowledgeRow(View):
    knowledge_id: str
    statement: str
    tier: str
    trusted: bool
    promoted_to_hard_gold: bool = False
    promoted_at: str | None = None
    evidence_refs: tuple[str, ...] = ()


class KnowledgeAndHardGoldPromotion(View):
    rows: tuple[KnowledgeRow, ...] = ()
    hard_gold_count: int = 0
    below_trust_floor_ids: tuple[str, ...] = ()


# 11 --------------------------------------------------------------------------
class ProvenanceEdgeView(View):
    source: str
    relation: str
    target: str
    terminus_commit: str | None = None
    repository_commit: str | None = None
    content_hash: str | None = None


class ProvenanceGraph(View):
    edges: tuple[ProvenanceEdgeView, ...] = ()
    node_count: int = 0
    #: Section 18: an artifact with no commit binding is not verified evidence.
    edges_without_commit_binding: int = 0


# 12 --------------------------------------------------------------------------
class ReleaseReadiness(View):
    release_id: str | None = None
    candidate_commit: str | None = None
    ready: bool = False
    gates_required: tuple[str, ...] = ()
    gates_passed: tuple[str, ...] = ()
    gates_outstanding: tuple[str, ...] = ()
    blocking_reason: str | None = None


# 13 --------------------------------------------------------------------------
class TypedBlocker(View):
    subject: str
    blocker: str
    owner_interrupt: OwnerInterrupt | None = None
    detail: str = ""


class BlockerAndOwnerDecision(View):
    project_state: ProjectState
    blockers: tuple[TypedBlocker, ...] = ()
    requested_owner_decision: TypedBlocker | None = None
    awaiting_owner: bool = False


# -----------------------------------------------------------------------------
class DashboardProjection(View):
    """All thirteen views for one project, from one consistent snapshot."""

    project_id: str
    contract_id: str
    contract_version: str
    captured_at: str | None = None
    terminus_commit: str | None = None

    project_and_milestone_status: ProjectAndMilestoneStatus
    task_ledger_and_critical_path: TaskLedgerAndCriticalPath
    task_ownership_leases_worktrees_and_stale_sessions: TaskOwnershipAndLeases
    contract_and_requirement_traceability: ContractAndRequirementTraceability
    scope_drift_findings: ScopeDriftFindings
    model_run_aliases_and_role_history: ModelRunAliasesAndRoleHistory
    visible_and_hidden_evaluation_status: VisibleAndHiddenEvaluationStatus
    oracle_health_and_mutant_results: OracleHealthAndMutantResults
    dependency_versions_and_impact_maps: DependencyVersionsAndImpactMaps
    knowledge_and_hard_gold_promotion_state: KnowledgeAndHardGoldPromotion
    provenance_graph: ProvenanceGraph
    release_readiness: ReleaseReadiness
    exact_typed_blocker_and_requested_owner_decision: BlockerAndOwnerDecision

    def view(self, name: str) -> View:
        if name not in REQUIRED_VIEWS:
            raise KeyError(f"{name!r} is not one of the thirteen Section 11.6 views")
        return getattr(self, name)
