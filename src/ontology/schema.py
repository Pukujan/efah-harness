"""The control-plane ontology: contract Section 9.1's forty required entities.

Two representations, one source of truth:

* **Pydantic models** -- what the harness manipulates in process. ``extra="forbid"``
  everywhere, so a field the contract does not define cannot be smuggled in.
* **TerminusDB JSON-LD schema documents** -- generated from those same models by
  :mod:`ontology.jsonld`. Writing the JSON-LD by hand would let the two drift,
  and a drifted schema is exactly the ``FAILED_PROVENANCE`` shape GATE-D1-02
  exists to catch.

Every entity inherits :class:`ControlPlaneEntity`, which carries ``entity_id``
and the Section 8 :class:`~governance.envelope.Envelope`. In TerminusDB that
becomes an abstract class, which lets :class:`Dependency` link *any* entity to
*any* entity with real referential integrity rather than with untyped strings.
The abstract parent does **not** propagate ``@key`` to its children (measured on
12.0.6 -- children without their own ``@key`` silently get random ids), so the
generator emits an explicit ``Lexical`` key on every concrete class.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from governance.envelope import Envelope
from governance.states import (
    ContractReviewOutcome,
    FailureClass,
    OwnerInterrupt,
    ProjectState,
    TaskState,
    Verdict,
)
from governance.envelope import EvidenceTier, KnowledgeTier

__all__ = [
    "Link",
    "ControlPlaneEntity",
    "DependencyEdgeType",
    "DependencyKind",
    "TaskEventType",
    "ENTITY_MODELS",
    "CONTROL_PLANE_ENTITY_NAMES",
    "LEDGER_MODELS",
    "ALL_MODELS",
    "document_id",
]


class Link:
    """Annotation marking a ``str`` field as a TerminusDB document link.

    ``Annotated[str, Link("Task")]`` is a link to a ``Task`` document; the Python
    value is the document id (``"Task/T-001"``).
    """

    __slots__ = ("target",)

    def __init__(self, target: str) -> None:
        self.target = target

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Link({self.target!r})"


#: A link to any control-plane entity. Section 9.6's dependency map is
#: heterogeneous -- a requirement may be ``implemented_by`` a task and a task
#: ``supported_by`` an artifact -- so the edge endpoints target the abstract
#: parent rather than a union of forty concrete classes.
AnyEntity = Annotated[str, Link("ControlPlaneEntity")]

_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


class DependencyEdgeType(StrEnum):
    """Contract Section 9.6 required edge types. This list is closed."""

    depends_on = "depends_on"
    blocks = "blocks"
    supported_by = "supported_by"
    derived_from = "derived_from"
    implemented_by = "implemented_by"
    tested_by = "tested_by"
    verified_by = "verified_by"
    evaluated_by = "evaluated_by"
    invalidated_by = "invalidated_by"
    supersedes = "supersedes"
    compatible_with = "compatible_with"
    conflicts_with = "conflicts_with"
    produced_by = "produced_by"
    deployed_to = "deployed_to"


class DependencyKind(StrEnum):
    """Which of Section 9.6's nine dependency planes an edge belongs to."""

    task = "task"
    requirement = "requirement"
    artifact = "artifact"
    software = "software"
    service = "service"
    documentation = "documentation"
    evaluation = "evaluation"
    deployment = "deployment"
    knowledge = "knowledge"


class TaskEventType(StrEnum):
    """Contract Section 9.2 append-only task-ledger events."""

    TaskCreated = "TaskCreated"
    TaskReady = "TaskReady"
    TaskAssigned = "TaskAssigned"
    LeaseAcquired = "LeaseAcquired"
    LeaseRenewed = "LeaseRenewed"
    WorkerStarted = "WorkerStarted"
    ToolCallRecorded = "ToolCallRecorded"
    ArtifactSubmitted = "ArtifactSubmitted"
    EvaluationStarted = "EvaluationStarted"
    GatePassed = "GatePassed"
    GateFailed = "GateFailed"
    TaskReworked = "TaskReworked"
    TaskBlocked = "TaskBlocked"
    TaskCompleted = "TaskCompleted"
    TaskMerged = "TaskMerged"
    TaskClosed = "TaskClosed"


class OwnershipMode(StrEnum):
    exclusive = "exclusive"
    shared = "shared"


class LeaseState(StrEnum):
    active = "active"
    renewed = "renewed"
    expired = "expired"
    superseded = "superseded"
    released = "released"


class PromotionState(StrEnum):
    proposed = "proposed"
    quarantined = "quarantined"
    reproduced = "reproduced"
    independently_verified = "independently_verified"
    promoted = "promoted"
    rejected = "rejected"


class ControlPlaneEntity(BaseModel):
    """Abstract parent of every Section 9.1 entity.

    ``envelope`` is the Section 8 header. It is required: an entity without one
    cannot be provenance-bound, and Section 8.1 forbids filling a material field
    with a silent default.
    """

    # ``protected_namespaces=()`` because ``ModelRun.model_alias`` is contract
    # vocabulary (Section 11.1), not pydantic's ``model_`` namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    entity_id: str
    envelope: Envelope

    @field_validator("entity_id")
    @classmethod
    def _id_is_document_safe(cls, value: str) -> str:
        if not _ENTITY_ID_RE.match(value):
            raise ValueError(
                f"entity_id {value!r} must match {_ENTITY_ID_RE.pattern}: TerminusDB percent-encodes "
                "anything else into the Lexical key, which breaks link round-tripping"
            )
        return value

    @property
    def document_id(self) -> str:
        return f"{type(self).__name__}/{self.entity_id}"


def document_id(model: ControlPlaneEntity) -> str:
    return model.document_id


# ---------------------------------------------------------------------------
# Section 9.1 -- the forty required entities
# ---------------------------------------------------------------------------


class Project(ControlPlaneEntity):
    name: str
    mode: str
    state: ProjectState
    pack_manifest_hash: str
    contract: Annotated[str, Link("Contract")] | None = None
    repositories: list[str] = Field(default_factory=list)


class ProjectVersion(ControlPlaneEntity):
    project: Annotated[str, Link("Project")]
    version: str
    pack_manifest_hash: str
    compiled_at: datetime
    supersedes: Annotated[str, Link("ProjectVersion")] | None = None


class ProjectPack(ControlPlaneEntity):
    root_path: str
    manifest_hash: str
    required_files_present: bool
    file_manifest: dict[str, Any]
    imported_at: datetime


class Contract(ControlPlaneEntity):
    contract_key: str
    title: str
    current_version: str


class ContractVersion(ControlPlaneEntity):
    contract: Annotated[str, Link("Contract")]
    version: str
    content_hash: str
    approved_by: str
    approved_at: datetime
    supersedes: Annotated[str, Link("ContractVersion")] | None = None


class Requirement(ControlPlaneEntity):
    contract_version: Annotated[str, Link("ContractVersion")]
    statement: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    normative_strength: str = "MUST"
    section_ref: str | None = None


class Methodology(ControlPlaneEntity):
    methodology_key: str
    name: str
    category: str
    mechanization: str


class MethodologyVersion(ControlPlaneEntity):
    methodology: Annotated[str, Link("Methodology")]
    version: str
    content_hash: str


class Workstream(ControlPlaneEntity):
    project: Annotated[str, Link("Project")]
    name: str
    owner_alias: str
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)


class Milestone(ControlPlaneEntity):
    project: Annotated[str, Link("Project")]
    name: str
    day: int
    achieved: bool = False


class Phase(ControlPlaneEntity):
    project: Annotated[str, Link("Project")]
    name: str
    ordinal: int
    required_outputs: list[str] = Field(default_factory=list)
    pass_condition: str
    failure_condition: str


class Task(ControlPlaneEntity):
    """Section 9.3 state lives here; transitions are enforced by the ledger."""

    project: Annotated[str, Link("Project")]
    workstream: Annotated[str, Link("Workstream")] | None = None
    phase: Annotated[str, Link("Phase")] | None = None
    title: str
    objective: str
    state: TaskState
    requirement_ids: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)
    assigned_alias: str | None = None
    risk_class: str = "standard"


class WorkUnit(ControlPlaneEntity):
    """Contract Section 9.4 work-unit success/failure schema."""

    task: Annotated[str, Link("Task")]
    objective: str
    requirement_ids: list[str] = Field(default_factory=list)
    contract_version: str
    methodology_ids: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    success_conditions: dict[str, Any] = Field(default_factory=dict)
    failure_conditions: list[str] = Field(default_factory=list)
    next_permitted_actions: list[str] = Field(default_factory=list)


class Assignment(ControlPlaneEntity):
    work_unit: Annotated[str, Link("WorkUnit")]
    role: str
    alias: str
    ownership_mode: OwnershipMode
    assigned_at: datetime


class AssignmentLease(ControlPlaneEntity):
    """Section 9.5. ``generation`` is the fencing token; a submission from a
    superseded generation is stale and must be rejected."""

    assignment: Annotated[str, Link("Assignment")]
    generation: int
    acquired_at: datetime
    expires_at: datetime
    renewed_at: datetime | None = None
    state: LeaseState = LeaseState.active
    repository: str
    branch: str
    worktree: str | None = None
    input_hashes: list[str] = Field(default_factory=list)
    permitted_output_schemas: list[str] = Field(default_factory=list)


class Dependency(ControlPlaneEntity):
    """Section 9.6 typed edge between any two control-plane entities."""

    edge_type: DependencyEdgeType
    kind: DependencyKind
    source: AnyEntity
    target: AnyEntity
    rationale: str | None = None


class Blocker(ControlPlaneEntity):
    interrupt_type: OwnerInterrupt
    description: str
    task: Annotated[str, Link("Task")] | None = None
    raised_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None


class Decision(ControlPlaneEntity):
    decision_key: str
    title: str
    status: str
    rationale: str
    alternatives_considered: list[str] = Field(default_factory=list)
    decided_by: str
    decided_at: datetime
    supersedes: Annotated[str, Link("Decision")] | None = None


class Assumption(ControlPlaneEntity):
    statement: str
    confidence: str
    validated: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class Risk(ControlPlaneEntity):
    title: str
    likelihood: str
    impact: str
    mitigation: str
    owner_alias: str
    status: str = "open"


class ChangeRequest(ControlPlaneEntity):
    title: str
    requested_by: str
    rationale: str
    impacted_requirements: list[str] = Field(default_factory=list)
    status: str = "proposed"


class Artifact(ControlPlaneEntity):
    """Section 18 requires content hash, producer, source inputs, links, location."""

    path: str
    artifact_type: str
    content_hash: str
    producer_alias: str
    storage_location: str
    produced_by_task: Annotated[str, Link("Task")] | None = None
    source_input_hashes: list[str] = Field(default_factory=list)


class SchemaVersion(ControlPlaneEntity):
    schema_key: str
    version: str
    content_hash: str
    applies_to: list[str] = Field(default_factory=list)


class ConfigurationVersion(ControlPlaneEntity):
    component: str
    version: str
    configuration_hash: str
    environment: Annotated[str, Link("Environment")] | None = None


class DependencyVersion(ControlPlaneEntity):
    """Contract Section 16.3 dependency-registry required fields."""

    component: str
    exact_version: str
    lockfile_source: str
    image_digest: str | None = None
    documentation_snapshot_id: str | None = None
    configuration_hash: str | None = None
    modules_using: list[str] = Field(default_factory=list)
    compatibility_constraints: list[str] = Field(default_factory=list)
    last_verified_run: datetime | None = None
    update_and_rollback_policy: str
    affected_tests: list[str] = Field(default_factory=list)


class Environment(ControlPlaneEntity):
    name: str
    kind: str
    endpoints: dict[str, Any] = Field(default_factory=dict)
    is_protected: bool = False


class ModelAlias(ControlPlaneEntity):
    """Section 11.1/11.2: an alias only. The real vendor/model identity is never
    stored on this side of the wall -- see :mod:`integrations.protected_identity`."""

    alias: str
    role: str
    gateway: str
    capability_ids: list[str] = Field(default_factory=list)
    gate_bearing: bool = False


class ModelCapability(ControlPlaneEntity):
    capability_key: str
    description: str
    required_for_roles: list[str] = Field(default_factory=list)


class ModelRun(ControlPlaneEntity):
    model_alias: Annotated[str, Link("ModelAlias")]
    role: str
    gateway: str
    configuration_hash: str
    input_hash: str
    output_hash: str | None = None
    protected_identity_ref: str
    task: Annotated[str, Link("Task")] | None = None
    started_at: datetime
    finished_at: datetime | None = None
    failure_class: FailureClass | None = None


class EvaluationRun(ControlPlaneEntity):
    target_artifact: Annotated[str, Link("Artifact")] | None = None
    oracle_version: Annotated[str, Link("OracleVersion")] | None = None
    environment: Annotated[str, Link("Environment")] | None = None
    mode: str
    holdout_visibility: str
    verdict: Verdict | None = None
    evidence_tier: EvidenceTier | None = None
    started_at: datetime
    finished_at: datetime | None = None


class Oracle(ControlPlaneEntity):
    oracle_key: str
    name: str
    oracle_type: str
    deterministic: bool
    model_judge_in_verdict_path: bool = False


class OracleVersion(ControlPlaneEntity):
    oracle: Annotated[str, Link("Oracle")]
    version: str
    content_hash: str
    health_status: str = "unknown"


class Holdout(ControlPlaneEntity):
    holdout_key: str
    visibility_class: str
    sealed: bool = True
    owner_identity: str


class Mutant(ControlPlaneEntity):
    mutant_key: str
    mutation_kind: str
    target_artifact: Annotated[str, Link("Artifact")] | None = None
    expected_killed: bool = True
    killed: bool | None = None


class GoldCandidate(ControlPlaneEntity):
    task: Annotated[str, Link("Task")] | None = None
    promotion_state: PromotionState = PromotionState.proposed
    evidence_refs: list[str] = Field(default_factory=list)
    reproducibility_status: str = "unknown"
    contamination_reviewed: bool = False


class GoldCase(ControlPlaneEntity):
    gold_key: str
    promoted_from: Annotated[str, Link("GoldCandidate")] | None = None
    trust_tier: KnowledgeTier
    content_hash: str
    contamination_policy: str


class KnowledgeCandidate(ControlPlaneEntity):
    statement: str
    knowledge_tier: KnowledgeTier
    promotion_state: PromotionState = PromotionState.proposed
    evidence_refs: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(default_factory=list)


class ContractReview(ControlPlaneEntity):
    contract_version: Annotated[str, Link("ContractVersion")]
    trigger: str
    outcome: ContractReviewOutcome
    findings: list[str] = Field(default_factory=list)
    reviewer_alias: str
    reviewed_at: datetime


class ReleaseCandidate(ControlPlaneEntity):
    commit_sha: str
    artifact_digest: str
    gate_results: dict[str, Any] = Field(default_factory=dict)
    provenance_attestation: str | None = None
    state: str = "proposed"


class DeploymentRun(ControlPlaneEntity):
    release_candidate: Annotated[str, Link("ReleaseCandidate")]
    environment: Annotated[str, Link("Environment")]
    mode: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    rollback_performed: bool = False


# ---------------------------------------------------------------------------
# Task ledger (Section 9.2) and time tracking (Section 9.8)
#
# Not in the Section 9.1 entity list, but the ledger has to be persisted in the
# same authoritative graph or "append-only" is a claim with nothing behind it.
# ---------------------------------------------------------------------------


class TaskEvent(ControlPlaneEntity):
    """One append-only ledger entry. Never updated, never deleted.

    ``sequence`` is per-task and monotonic; ``recorded_at`` comes from the system
    clock at append time (Section 9.8: system events, not agent estimates).
    """

    task: Annotated[str, Link("Task")]
    sequence: int
    event_type: TaskEventType
    actor_alias: str
    actor_role: str
    recorded_at: datetime
    resulting_state: TaskState | None = None
    lease_generation: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskProjection(ControlPlaneEntity):
    """Current-state projection folded from :class:`TaskEvent` (Section 9.2).

    Derived, never authored: rebuilding it from the event stream must reproduce
    it exactly, which is what makes the ledger the authority rather than this.
    """

    task: Annotated[str, Link("Task")]
    state: TaskState
    event_count: int
    last_event_type: TaskEventType
    last_event_at: datetime
    assigned_alias: str | None = None
    lease_generation: int | None = None
    gate_failures: int = 0
    rework_count: int = 0

    queued_at: datetime | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    candidate_submitted_at: datetime | None = None
    verification_started_at: datetime | None = None
    blocked_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_at: datetime | None = None
    merged_at: datetime | None = None

    queue_seconds: float | None = None
    active_seconds: float | None = None
    blocked_seconds: float | None = None
    evaluation_seconds: float | None = None
    rework_seconds: float | None = None
    total_wall_clock_seconds: float | None = None


#: Contract Section 9.1, in contract order. Length is asserted by the tests.
ENTITY_MODELS: tuple[type[ControlPlaneEntity], ...] = (
    Project,
    ProjectVersion,
    ProjectPack,
    Contract,
    ContractVersion,
    Requirement,
    Methodology,
    MethodologyVersion,
    Workstream,
    Milestone,
    Phase,
    Task,
    WorkUnit,
    Assignment,
    AssignmentLease,
    Dependency,
    Blocker,
    Decision,
    Assumption,
    Risk,
    ChangeRequest,
    Artifact,
    SchemaVersion,
    ConfigurationVersion,
    DependencyVersion,
    Environment,
    ModelAlias,
    ModelCapability,
    ModelRun,
    EvaluationRun,
    Oracle,
    OracleVersion,
    Holdout,
    Mutant,
    GoldCandidate,
    GoldCase,
    KnowledgeCandidate,
    ContractReview,
    ReleaseCandidate,
    DeploymentRun,
)

LEDGER_MODELS: tuple[type[ControlPlaneEntity], ...] = (TaskEvent, TaskProjection)

ALL_MODELS: tuple[type[ControlPlaneEntity], ...] = ENTITY_MODELS + LEDGER_MODELS

CONTROL_PLANE_ENTITY_NAMES: tuple[str, ...] = tuple(m.__name__ for m in ENTITY_MODELS)
