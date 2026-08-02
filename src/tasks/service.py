"""Task lifecycle over the ledger (contract Sections 9.2, 9.3, 9.5, 9.8).

The :class:`~ontology.schema.Task` entity's ``state`` field is a *cache* of the
projection, not an independent authority: every transition here appends the
ledger event first, folds the stream, and only then writes the task's state from
what the fold produced. A caller therefore cannot set a state the event stream
does not support.

Lease fencing (Section 9.5) is enforced on submission: an
:class:`~ontology.schema.AssignmentLease` whose ``generation`` is behind the
task's current generation, or whose ``expires_at`` has passed, is refused with
:class:`StaleLeaseRejected` and recorded as a ``STALE_ASSIGNMENT`` state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from governance.envelope import Envelope
from governance.states import TaskState
from ontology.schema import (
    Assignment,
    AssignmentLease,
    LeaseState,
    OwnershipMode,
    Task,
    TaskEvent,
    TaskEventType,
    TaskProjection,
)
from provenance.writer import ProvenanceWriter, WriteReceipt
from tasks.ledger import Actor, ActorKind, TaskLedger

__all__ = ["StaleLeaseRejected", "TaskCreation", "TaskService"]


class StaleLeaseRejected(RuntimeError):
    """Section 9.5: a submission from an expired or superseded lease."""


@dataclass(frozen=True)
class TaskCreation:
    task: Task
    event: TaskEvent
    receipts: tuple[WriteReceipt, ...]


def _system_clock() -> datetime:
    return datetime.now(UTC)


class TaskService:
    def __init__(
        self,
        writer: ProvenanceWriter,
        *,
        ledger: TaskLedger | None = None,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._ledger = ledger or TaskLedger(writer, clock=clock)

    @property
    def ledger(self) -> TaskLedger:
        return self._ledger

    async def create_task(
        self,
        *,
        entity_id: str,
        project_document_id: str,
        title: str,
        objective: str,
        actor: Actor,
        workstream_document_id: str | None = None,
        requirement_ids: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        prohibited_paths: list[str] | None = None,
        risk_class: str = "standard",
    ) -> TaskCreation:
        task = Task(
            entity_id=entity_id,
            envelope=Envelope(schema_id="efah.task", created_by_alias=actor.alias),
            project=project_document_id,
            workstream=workstream_document_id,
            title=title,
            objective=objective,
            state=TaskState.PROPOSED,
            requirement_ids=list(requirement_ids or []),
            allowed_paths=list(allowed_paths or []),
            prohibited_paths=list(prohibited_paths or []),
            risk_class=risk_class,
        )
        task_receipt = await self._writer.write([task], message=f"create task {entity_id}: {title}")
        event, event_receipt = await self._ledger.append(
            task.document_id, TaskEventType.TaskCreated, actor, payload={"title": title}
        )
        return TaskCreation(task=task, event=event, receipts=(task_receipt, event_receipt))

    async def record(
        self,
        task_document_id: str,
        event_type: TaskEventType,
        actor: Actor,
        *,
        payload: dict[str, Any] | None = None,
        lease: AssignmentLease | None = None,
    ) -> tuple[TaskEvent, TaskProjection]:
        """Append an event, then refresh the task state and its projection."""
        if lease is not None:
            self.assert_lease_valid(lease)
        event, _ = await self._ledger.append(
            task_document_id,
            event_type,
            actor,
            payload=payload,
            lease_generation=lease.generation if lease else None,
        )
        projection, _ = await self._ledger.persist_projection(task_document_id)
        await self._sync_task_state(task_document_id, projection)
        return event, projection

    async def submit_candidate(
        self,
        task_document_id: str,
        actor: Actor,
        *,
        lease: AssignmentLease,
        artifact_ids: list[str],
    ) -> tuple[TaskEvent, TaskProjection]:
        """A worker's only route to ``CANDIDATE_COMPLETE`` (Section 9.3).

        Rejects a stale lease before anything is appended, so a fenced-out worker
        leaves no ledger trace beyond the rejection the caller records.
        """
        if actor.kind is not ActorKind.WORKER:
            raise StaleLeaseRejected(
                f"submit_candidate is the worker path; {actor.alias!r} is a {actor.kind}"
            )
        self.assert_lease_valid(lease)
        return await self.record(
            task_document_id,
            TaskEventType.ArtifactSubmitted,
            actor,
            payload={"artifact_ids": list(artifact_ids)},
            lease=lease,
        )

    def assert_lease_valid(self, lease: AssignmentLease) -> None:
        now = self._clock()
        if lease.state in (LeaseState.expired, LeaseState.superseded, LeaseState.released):
            raise StaleLeaseRejected(
                f"lease {lease.entity_id} is {lease.state}; submission is STALE_ASSIGNMENT"
            )
        expires = lease.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            raise StaleLeaseRejected(
                f"lease {lease.entity_id} expired at {expires.isoformat()}; "
                "submission is STALE_ASSIGNMENT"
            )

    async def acquire_lease(
        self,
        *,
        task_document_id: str,
        work_unit_document_id: str,
        alias: str,
        role: str,
        repository: str,
        branch: str,
        ttl_seconds: int,
        worktree: str | None = None,
        input_hashes: list[str] | None = None,
        ownership_mode: OwnershipMode = OwnershipMode.exclusive,
    ) -> tuple[Assignment, AssignmentLease]:
        """Create the assignment and its fencing lease, then record the events."""
        now = self._clock()
        system = Actor(alias="system", role="control-plane", kind=ActorKind.SYSTEM)
        task_key = task_document_id.split("/", 1)[-1]

        previous = [
            lease
            for lease in await self._writer.read_all(AssignmentLease)
            if isinstance(lease, AssignmentLease) and lease.entity_id.startswith(f"LEASE-{task_key}-")
        ]
        generation = max((lease.generation for lease in previous), default=0) + 1

        assignment = Assignment(
            entity_id=f"ASSIGN-{task_key}-{generation:04d}",
            envelope=Envelope(schema_id="efah.assignment", created_by_alias=alias),
            work_unit=work_unit_document_id,
            role=role,
            alias=alias,
            ownership_mode=ownership_mode,
            assigned_at=now,
        )
        lease = AssignmentLease(
            entity_id=f"LEASE-{task_key}-{generation:04d}",
            envelope=Envelope(schema_id="efah.assignment_lease", created_by_alias=alias),
            assignment=assignment.document_id,
            generation=generation,
            acquired_at=now,
            expires_at=now.fromtimestamp(now.timestamp() + ttl_seconds, tz=UTC),
            state=LeaseState.active,
            repository=repository,
            branch=branch,
            worktree=worktree,
            input_hashes=list(input_hashes or []),
        )
        await self._writer.write(
            [assignment, lease], message=f"lease generation {generation} for {task_document_id}"
        )
        await self.record(
            task_document_id,
            TaskEventType.TaskAssigned,
            system,
            payload={"assigned_alias": alias, "role": role},
            lease=lease,
        )
        await self.record(
            task_document_id,
            TaskEventType.LeaseAcquired,
            system,
            payload={"lease": lease.document_id, "branch": branch},
            lease=lease,
        )
        return assignment, lease

    async def _sync_task_state(self, task_document_id: str, projection: TaskProjection) -> None:
        entity_id = task_document_id.split("/", 1)[-1]
        task = await self._writer.read(Task, entity_id)
        if not isinstance(task, Task):
            return
        if task.state == projection.state and task.assigned_alias == projection.assigned_alias:
            return
        updated = task.model_copy(
            update={"state": projection.state, "assigned_alias": projection.assigned_alias}
        )
        await self._writer.write(
            [updated],
            message=f"task {entity_id} state <- projection: {projection.state}",
            upsert=True,
        )
