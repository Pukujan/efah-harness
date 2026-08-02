"""Typed LangGraph state model.

Contract EFAH-CONTRACT-001 v1.1 Section 10.4: *every* graph checkpoint MUST
reference project ID, project version, contract version, the TerminusDB
database/branch/commit, the work unit, the current graph node, the assignment
lease generation, input and output hashes, and pending gates plus typed
blockers.

The list is enforced mechanically by the checkpoint adapter
(:mod:`workflows.checkpoint`), not by reviewer diligence: a checkpoint that
carries graph state but omits one of the twelve fields is refused at write time.
Section 10.1 draws the boundary the other way round as well -- the checkpoint is
*execution* state. TerminusDB remains the authority for what is true. Deleting
the checkpoint store loses progress, never truth.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

#: Contract Section 10.4. Closed list; the adapter refuses anything short of it.
REQUIRED_CHECKPOINT_FIELDS: tuple[str, ...] = (
    "project_id",
    "project_version",
    "contract_version",
    "terminus_database",
    "terminus_branch",
    "terminus_commit",
    "work_unit_id",
    "graph_node",
    "lease_generation",
    "input_hashes",
    "output_hashes",
    "pending_gates",
)

#: Channels LangGraph manages itself. A checkpoint that holds only these is an
#: input-staging or routing checkpoint and carries no graph state yet, so the
#: Section 10.4 assertion does not apply to it.
_FRAMEWORK_CHANNEL_PREFIXES: tuple[str, ...] = (
    "__start__",
    "__end__",
    "__previous__",
    "__interrupt__",
    "__root__",
    "branch:",
    "start:",
)


class MissingCheckpointFields(RuntimeError):
    """A checkpoint carried graph state but omitted Section 10.4 fields."""

    def __init__(self, missing: list[str], present: list[str]) -> None:
        self.missing = missing
        self.present = present
        super().__init__(
            "checkpoint omits contract Section 10.4 required fields "
            f"{missing}; present channels: {present}"
        )


def is_framework_channel(name: str) -> bool:
    return any(name == p or name.startswith(p) for p in _FRAMEWORK_CHANNEL_PREFIXES)


def merge_hashes(left: dict[str, str] | None, right: dict[str, str] | None) -> dict[str, str]:
    """Reducer for hash maps written by concurrent nodes.

    Concurrent branches contribute disjoint keys; a later value for the same key
    replaces the earlier one so a re-executed node is idempotent (Section 10.6).
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def merge_artifacts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def union_ordered(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Order-preserving set union. Used for gates and typed blockers.

    A gate must not appear twice because two parallel nodes both declared it
    pending, and it must not silently vanish because one of them wrote a shorter
    list.
    """
    out: list[str] = []
    for item in list(left or []) + list(right or []):
        if item not in out:
            out.append(item)
    return out


def last_write(left: str | None, right: str | None) -> str | None:
    """Reducer for scalar channels written by more than one concurrent node."""
    return right if right is not None else left


def max_int(left: int | None, right: int | None) -> int:
    """Lease generation only ever moves forward (Section 9.5)."""
    return max(left or 0, right or 0)


class WorkflowState(TypedDict, total=False):
    """The state schema shared by all twelve required graphs (Section 10.2).

    Sharing one schema is deliberate: ``project_graph`` composes the others as
    subgraphs, and a subgraph whose schema diverges cannot carry the Section
    10.4 references through the composition.
    """

    # --- Section 10.4 required checkpoint references -----------------------
    project_id: str
    project_version: str
    contract_version: str
    terminus_database: str
    terminus_branch: str
    terminus_commit: str
    work_unit_id: str
    graph_node: Annotated[str, last_write]
    lease_generation: Annotated[int, max_int]
    input_hashes: Annotated[dict[str, str], merge_hashes]
    output_hashes: Annotated[dict[str, str], merge_hashes]
    pending_gates: Annotated[list[str], union_ordered]

    # --- Section 10.4 "typed blockers", plus execution bookkeeping ---------
    typed_blockers: Annotated[list[str], union_ordered]
    graph_id: Annotated[str, last_write]
    node_log: Annotated[list[str], operator.add]
    artifacts: Annotated[dict[str, Any], merge_artifacts]
    gate_verdicts: Annotated[dict[str, Any], merge_artifacts]
    work_units: list[dict[str, Any]]
    failure_class: Annotated[str, last_write]
    project_state: Annotated[str, last_write]
    owner_interrupts: Annotated[list[str], union_ordered]


class CheckpointReference(BaseModel):
    """Typed view of the Section 10.4 field set.

    Used to validate a checkpoint's ``channel_values`` and to emit the field
    dump GATE-D1-04 lists under ``evidence_required``.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_version: str
    contract_version: str
    terminus_database: str
    terminus_branch: str
    terminus_commit: str
    work_unit_id: str
    graph_node: str
    lease_generation: int
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    pending_gates: list[str] = Field(default_factory=list)

    @classmethod
    def from_channel_values(cls, channel_values: dict[str, Any]) -> "CheckpointReference":
        return cls(**{k: channel_values[k] for k in REQUIRED_CHECKPOINT_FIELDS})


def missing_required_fields(channel_values: dict[str, Any]) -> list[str]:
    """Return the Section 10.4 fields absent from ``channel_values``.

    ``None`` counts as absent. A checkpoint that records ``terminus_commit:
    null`` cannot bind an artifact to a commit, which is exactly what Section
    10.4 exists to prevent.
    """
    return [
        name
        for name in REQUIRED_CHECKPOINT_FIELDS
        if name not in channel_values or channel_values[name] is None
    ]


def carries_graph_state(channel_values: dict[str, Any]) -> bool:
    """True when a checkpoint holds domain channels rather than only plumbing."""
    return any(not is_framework_channel(name) for name in channel_values)


def assert_checkpoint_fields(channel_values: dict[str, Any]) -> None:
    """Raise unless a state-carrying checkpoint satisfies Section 10.4."""
    if not carries_graph_state(channel_values):
        return
    missing = missing_required_fields(channel_values)
    if missing:
        raise MissingCheckpointFields(missing, sorted(channel_values))


def initial_state(
    *,
    project_id: str,
    project_version: str,
    contract_version: str,
    terminus_database: str,
    terminus_branch: str,
    terminus_commit: str,
    work_unit_id: str,
    graph_id: str,
    lease_generation: int = 0,
    input_hashes: dict[str, str] | None = None,
    pending_gates: list[str] | None = None,
) -> WorkflowState:
    """Build a run input that already satisfies Section 10.4.

    Every field is written at the input super-step, so every subsequent
    checkpoint inherits all twelve references rather than acquiring them
    part-way through a run.
    """
    return WorkflowState(
        project_id=project_id,
        project_version=project_version,
        contract_version=contract_version,
        terminus_database=terminus_database,
        terminus_branch=terminus_branch,
        terminus_commit=terminus_commit,
        work_unit_id=work_unit_id,
        graph_node="__input__",
        lease_generation=lease_generation,
        input_hashes=dict(input_hashes or {}),
        output_hashes={},
        pending_gates=list(pending_gates or []),
        typed_blockers=[],
        graph_id=graph_id,
        node_log=[],
        artifacts={},
        gate_verdicts={},
        work_units=[],
        failure_class="",
        project_state="RUNNING",
        owner_interrupts=[],
    )
