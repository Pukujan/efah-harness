"""Application read model of authoritative state.

These are the shapes controllers hand back and the dashboard projects. They are
deliberately *not* TerminusDB documents: contract Section 11.5 forbids
persistence-specific code in controllers, and Section 5.1 requires cross-module
operations to go through declared application interfaces. The TerminusDB
adapter (WS-B) maps documents onto these; the API never sees WOQL.

Everything here is frozen. A read model that can be mutated in place is a write
path with extra steps, and Section 5.1 says the dashboard must not have one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from governance.states import (
    DriftFinding,
    OwnerInterrupt,
    ProjectState,
    TaskState,
    Verdict,
)
from observability.identity import assert_alias_only


class ReadModel(BaseModel):
    """Frozen, closed-world base. ``extra='forbid'`` is the point.

    An adapter that quietly adds ``real_model_id`` to a record would otherwise
    sail straight through into a projection. Here it raises.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MilestoneRecord(ReadModel):
    milestone_id: str
    name: str
    state: str
    target_date: str | None = None
    completed_tasks: int = 0
    total_tasks: int = 0


class RequirementRecord(ReadModel):
    requirement_id: str
    statement: str
    contract_section: str
    #: Section 19.2 UNLINKED_TASK exists because this list can be empty.
    satisfied_by_task_ids: tuple[str, ...] = ()
    verified_by_gate_ids: tuple[str, ...] = ()


class LeaseRecord(ReadModel):
    """Section 9.5 ownership, leases, and stale-worker fencing."""

    lease_id: str
    task_id: str
    holder_alias: str | None = None
    worktree: str | None = None
    fence_token: int = 0
    acquired_at: str | None = None
    expires_at: str | None = None
    last_heartbeat_at: str | None = None
    is_stale: bool = False

    @field_validator("holder_alias")
    @classmethod
    def _alias_only(cls, value: str | None) -> str | None:
        return assert_alias_only(value, field="lease.holder_alias")


class TimingRecord(ReadModel):
    """Section 9.8. System-event timestamps only -- no agent estimate field.

    There is deliberately no ``estimated_hours``: the schema is the enforcement.
    """

    queued_at: str | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    last_heartbeat_at: str | None = None
    candidate_submitted_at: str | None = None
    verification_started_at: str | None = None
    blocked_at: str | None = None
    resumed_at: str | None = None
    completed_at: str | None = None
    merged_at: str | None = None


class WorkUnitRecord(ReadModel):
    work_unit_id: str
    task_id: str
    state: TaskState
    summary: str = ""
    timing: TimingRecord = Field(default_factory=TimingRecord)


class TaskRecord(ReadModel):
    task_id: str
    project_id: str
    title: str
    state: TaskState
    milestone_id: str | None = None
    workstream: str | None = None
    phase: str | None = None
    depends_on: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    on_critical_path: bool = False
    lease: LeaseRecord | None = None
    timing: TimingRecord = Field(default_factory=TimingRecord)
    work_units: tuple[WorkUnitRecord, ...] = ()
    typed_blocker: str | None = None
    owner_interrupt: OwnerInterrupt | None = None


class ModelRunRecord(ReadModel):
    """Section 11.2 / 12.3: alias and role only. No vendor field exists here."""

    run_id: str
    task_id: str | None = None
    model_alias: str
    role: str
    gateway_class: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None

    @field_validator("model_alias")
    @classmethod
    def _alias_only(cls, value: str) -> str:
        assert_alias_only(value, field="model_run.model_alias")
        return value


class EvaluationRecord(ReadModel):
    """Section 17. Visible AND hidden status, without protected content.

    There is no field for a holdout assertion, a private fixture, or a mutant
    implementation. ``hidden_suite_verdict`` carries the verdict and the counts
    the owner needs; the internals stay on the sealed side.
    """

    evaluation_id: str
    task_id: str | None = None
    project_id: str | None = None
    visible_verdict: Verdict | None = None
    visible_passed: int = 0
    visible_total: int = 0
    hidden_suite_verdict: Verdict | None = None
    hidden_suite_name: str | None = None
    hidden_assertions_total: int = 0
    hidden_assertions_failed: int = 0
    oracle_version: str | None = None
    failure_class: str | None = None
    evaluated_at: str | None = None


class OracleHealthRecord(ReadModel):
    oracle_id: str
    oracle_version: str
    healthy: bool
    last_checked_at: str | None = None
    #: Section 17.3/17.4: a mutant that survives means the oracle is not
    #: discriminating. The count is evidence; the mutant source is not shown.
    mutants_total: int = 0
    mutants_killed: int = 0
    mutants_survived: int = 0
    model_judge_in_verdict_path: bool = False


class DependencyRecord(ReadModel):
    """Section 16.3 dependency registry entry."""

    dependency_id: str
    component: str
    version: str
    lockfile_source: str | None = None
    image_digest: str | None = None
    documentation_snapshot_id: str | None = None
    configuration_hash: str | None = None
    used_by_modules: tuple[str, ...] = ()
    known_constraints: tuple[str, ...] = ()
    last_verified_run: str | None = None
    update_policy: str | None = None
    affected_gold_tests: tuple[str, ...] = ()


class KnowledgeRecord(ReadModel):
    """Section 15.5 / 15.6 knowledge tier and hard-gold promotion state."""

    knowledge_id: str
    statement: str
    tier: str
    promoted_to_hard_gold: bool = False
    promoted_at: str | None = None
    evidence_refs: tuple[str, ...] = ()


class ProvenanceEdge(ReadModel):
    """Section 18. One edge of the provenance graph."""

    source: str
    relation: str
    target: str
    terminus_commit: str | None = None
    repository_commit: str | None = None
    content_hash: str | None = None


class DriftFindingRecord(ReadModel):
    finding_id: str
    finding_type: DriftFinding
    detail: str
    subject: str | None = None
    detected_at: str | None = None
    resolved: bool = False


class DecisionRecord(ReadModel):
    decision_id: str
    title: str
    outcome: str
    decided_by: str
    decided_at: str
    contract_version: str
    link: str | None = None
    rationale: str = ""


class ReleaseRecord(ReadModel):
    release_id: str
    candidate_commit: str | None = None
    gates_required: tuple[str, ...] = ()
    gates_passed: tuple[str, ...] = ()
    blocking_gate_ids: tuple[str, ...] = ()
    ready: bool = False


class ProjectRecord(ReadModel):
    project_id: str
    name: str
    state: ProjectState
    contract_id: str
    contract_version: str
    pack_manifest_hash: str | None = None
    terminus_commit: str | None = None
    repository_commit: str | None = None
    imported_at: str | None = None
    current_run_id: str | None = None
    milestones: tuple[MilestoneRecord, ...] = ()


class ControlPlaneSnapshot(ReadModel):
    """One consistent read of authoritative state for one project.

    A snapshot rather than a live handle: the dashboard renders thirteen views,
    and thirteen independent reads of a moving graph would show thirteen
    mutually inconsistent stories.
    """

    project: ProjectRecord
    tasks: tuple[TaskRecord, ...] = ()
    requirements: tuple[RequirementRecord, ...] = ()
    model_runs: tuple[ModelRunRecord, ...] = ()
    evaluations: tuple[EvaluationRecord, ...] = ()
    oracles: tuple[OracleHealthRecord, ...] = ()
    dependencies: tuple[DependencyRecord, ...] = ()
    knowledge: tuple[KnowledgeRecord, ...] = ()
    provenance: tuple[ProvenanceEdge, ...] = ()
    drift_findings: tuple[DriftFindingRecord, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    release: ReleaseRecord | None = None
    captured_at: str | None = None
    terminus_commit: str | None = None

    def task(self, task_id: str) -> TaskRecord | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)


class RunHandle(ReadModel):
    """What a runtime port returns when a run is accepted."""

    run_id: str
    project_id: str
    task_id: str | None = None
    accepted: bool
    state: str
    detail: str = ""
    trace_id: str | None = None


class ImpactMap(ReadModel):
    """Section 16.2 / 9.6 dependency-change impact."""

    dependency_id: str
    version: str
    affected_modules: tuple[str, ...] = ()
    affected_task_ids: tuple[str, ...] = ()
    affected_requirement_ids: tuple[str, ...] = ()
    revalidation_gate_ids: tuple[str, ...] = ()
    affected_gold_tests: tuple[str, ...] = ()


class GraphView(ReadModel):
    """Section 9.6 project/task dependency graph."""

    project_id: str
    nodes: tuple[dict[str, Any], ...] = ()
    edges: tuple[dict[str, Any], ...] = ()
    critical_path: tuple[str, ...] = ()
    has_cycle: bool = False
