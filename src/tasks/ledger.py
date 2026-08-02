"""Append-only task ledger and current-state projection (contract Section 9.2).

Three rules are enforced here rather than documented and hoped for:

1. **Append-only.** ``sequence`` is dense and monotonic per task, assigned by the
   ledger from what is already in the graph. :meth:`TaskLedger.verify_append_only`
   re-reads the stream and checks density, monotonic timestamps, and each event's
   own content hash.

   The provenance writer's second commit rewrites only envelope metadata
   (``terminus_commit`` and the hash that covers it) -- never event content. The
   integrity check recomputes the hash over the *body*, so a rewrite of any
   ledger field would be caught.

2. **Section 9.3 authority.** "Workers may submit ``CANDIDATE_COMPLETE``. Only
   gates may produce ``PASSED``." An event whose resulting state is in
   ``GATE_ONLY_STATES`` and whose actor is not a gate raises
   :class:`LedgerAuthorityViolation`. A worker actor is confined to
   :data:`WORKER_AUTHORABLE_STATES`.

3. **Section 9.8 time from system events.** :meth:`TaskLedger.append` takes no
   timestamp argument. ``recorded_at`` comes from the ledger's clock at append
   time, so an agent cannot report how long it thinks it took. The clock is
   injectable only so tests can be deterministic -- it is still a system clock,
   not a claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from governance.envelope import Envelope
from governance.states import GATE_ONLY_STATES, WORKER_SUBMITTABLE_STATES, TaskState
from ontology.schema import TaskEvent, TaskEventType, TaskProjection
from provenance.writer import ProvenanceWriter, WriteReceipt

__all__ = [
    "ActorKind",
    "Actor",
    "LedgerAuthorityViolation",
    "LedgerIntegrityError",
    "TaskLedger",
    "EVENT_RESULTING_STATE",
    "EVENT_PERMITTED_ACTORS",
    "WORKER_AUTHORABLE_STATES",
]


class ActorKind(StrEnum):
    """Who is appending. Section 9.3 keys authority off this, not off the alias."""

    SYSTEM = "system"
    WORKER = "worker"
    GATE = "gate"
    OWNER = "owner"


@dataclass(frozen=True)
class Actor:
    alias: str
    role: str
    kind: ActorKind

    def __post_init__(self) -> None:
        if not self.alias.strip():
            raise LedgerAuthorityViolation("an actor alias is required (contract Section 18)")


class LedgerAuthorityViolation(RuntimeError):
    """A state or event the actor is not permitted to write (Section 9.3)."""


class LedgerIntegrityError(RuntimeError):
    """The stored event stream is not append-only."""


#: ``RUNNING`` joins the worker-writable set because Section 9.3's restriction is
#: about *completion* claims: a worker may say it started, and may submit
#: ``CANDIDATE_COMPLETE``, but ``PASSED``/``MERGED`` remain gate-only.
WORKER_AUTHORABLE_STATES = frozenset(WORKER_SUBMITTABLE_STATES | {TaskState.RUNNING})

#: Which task state each ledger event lands the task in. ``None`` means the event
#: is observational (a tool call does not change state).
EVENT_RESULTING_STATE: dict[TaskEventType, TaskState | None] = {
    TaskEventType.TaskCreated: TaskState.PROPOSED,
    TaskEventType.TaskReady: TaskState.READY,
    TaskEventType.TaskAssigned: TaskState.CLAIMED,
    TaskEventType.LeaseAcquired: TaskState.CLAIMED,
    TaskEventType.LeaseRenewed: None,
    TaskEventType.WorkerStarted: TaskState.RUNNING,
    TaskEventType.ToolCallRecorded: None,
    TaskEventType.ArtifactSubmitted: TaskState.CANDIDATE_COMPLETE,
    TaskEventType.EvaluationStarted: TaskState.VERIFYING,
    TaskEventType.GatePassed: TaskState.PASSED,
    TaskEventType.GateFailed: TaskState.FAILED_VISIBLE_TEST,
    TaskEventType.TaskReworked: TaskState.REWORK_REQUIRED,
    TaskEventType.TaskBlocked: TaskState.BLOCKED_DEPENDENCY,
    TaskEventType.TaskCompleted: TaskState.PASSED,
    TaskEventType.TaskMerged: TaskState.MERGED,
    TaskEventType.TaskClosed: TaskState.CLOSED,
}

#: Which actor kinds may author each event. A worker cannot fabricate a gate
#: result by choosing a different event type.
EVENT_PERMITTED_ACTORS: dict[TaskEventType, frozenset[ActorKind]] = {
    TaskEventType.TaskCreated: frozenset({ActorKind.SYSTEM, ActorKind.OWNER}),
    TaskEventType.TaskReady: frozenset({ActorKind.SYSTEM}),
    TaskEventType.TaskAssigned: frozenset({ActorKind.SYSTEM}),
    TaskEventType.LeaseAcquired: frozenset({ActorKind.SYSTEM}),
    TaskEventType.LeaseRenewed: frozenset({ActorKind.SYSTEM, ActorKind.WORKER}),
    TaskEventType.WorkerStarted: frozenset({ActorKind.WORKER}),
    TaskEventType.ToolCallRecorded: frozenset({ActorKind.WORKER, ActorKind.SYSTEM}),
    TaskEventType.ArtifactSubmitted: frozenset({ActorKind.WORKER}),
    TaskEventType.EvaluationStarted: frozenset({ActorKind.GATE, ActorKind.SYSTEM}),
    TaskEventType.GatePassed: frozenset({ActorKind.GATE}),
    TaskEventType.GateFailed: frozenset({ActorKind.GATE}),
    TaskEventType.TaskReworked: frozenset({ActorKind.SYSTEM, ActorKind.GATE, ActorKind.WORKER}),
    TaskEventType.TaskBlocked: frozenset({ActorKind.SYSTEM, ActorKind.WORKER, ActorKind.GATE}),
    TaskEventType.TaskCompleted: frozenset({ActorKind.GATE}),
    TaskEventType.TaskMerged: frozenset({ActorKind.SYSTEM, ActorKind.GATE}),
    TaskEventType.TaskClosed: frozenset({ActorKind.SYSTEM, ActorKind.GATE, ActorKind.OWNER}),
}

_HEARTBEAT_EVENTS = frozenset(
    {
        TaskEventType.WorkerStarted,
        TaskEventType.LeaseRenewed,
        TaskEventType.ToolCallRecorded,
        TaskEventType.ArtifactSubmitted,
    }
)


def _system_clock() -> datetime:
    return datetime.now(UTC)


class TaskLedger:
    """Reads and appends the Section 9.2 event stream for one project graph."""

    def __init__(
        self,
        writer: ProvenanceWriter,
        *,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        self._writer = writer
        self._clock = clock

    @property
    def writer(self) -> ProvenanceWriter:
        return self._writer

    # -- authority ---------------------------------------------------------

    @staticmethod
    def check_authority(event_type: TaskEventType, actor: Actor, state: TaskState | None) -> None:
        """Raise unless *actor* may author this event and land this state."""
        permitted = EVENT_PERMITTED_ACTORS.get(event_type, frozenset())
        if actor.kind not in permitted:
            raise LedgerAuthorityViolation(
                f"actor kind {actor.kind} may not author {event_type}; "
                f"permitted: {sorted(k.value for k in permitted)}"
            )
        if state is None:
            return
        if state in GATE_ONLY_STATES and actor.kind is not ActorKind.GATE:
            raise LedgerAuthorityViolation(
                f"contract Section 9.3: only gates may produce {state}; "
                f"{actor.alias!r} is a {actor.kind}"
            )
        if actor.kind is ActorKind.WORKER and state not in WORKER_AUTHORABLE_STATES:
            raise LedgerAuthorityViolation(
                f"contract Section 9.3: a worker may submit "
                f"{sorted(s.value for s in WORKER_AUTHORABLE_STATES)}, not {state}"
            )

    # -- append ------------------------------------------------------------

    async def append(
        self,
        task_document_id: str,
        event_type: TaskEventType,
        actor: Actor,
        *,
        payload: dict[str, Any] | None = None,
        lease_generation: int | None = None,
        resulting_state: TaskState | None = None,
    ) -> tuple[TaskEvent, WriteReceipt]:
        """Append one event. There is no timestamp parameter, by design."""
        state = resulting_state if resulting_state is not None else EVENT_RESULTING_STATE[event_type]
        self.check_authority(event_type, actor, state)

        existing = await self.events(task_document_id)
        sequence = (existing[-1].sequence + 1) if existing else 1
        task_key = task_document_id.split("/", 1)[-1]

        event = TaskEvent(
            entity_id=f"EV-{task_key}-{sequence:06d}",
            envelope=Envelope(schema_id="efah.task_event", created_by_alias=actor.alias),
            task=task_document_id,
            sequence=sequence,
            event_type=event_type,
            actor_alias=actor.alias,
            actor_role=actor.role,
            recorded_at=self._clock(),
            resulting_state=state,
            lease_generation=lease_generation,
            payload=dict(payload or {}),
        )
        receipt = await self._writer.write(
            [event], message=f"{event_type} on {task_document_id} by {actor.alias}"
        )
        return event, receipt

    # -- read --------------------------------------------------------------

    async def events(self, task_document_id: str) -> list[TaskEvent]:
        """The full stream for one task, ordered by ``sequence``."""
        all_events = await self._writer.read_all(TaskEvent)
        stream = [e for e in all_events if isinstance(e, TaskEvent) and e.task == task_document_id]
        return sorted(stream, key=lambda e: e.sequence)

    async def verify_append_only(self, task_document_id: str) -> bool:
        """Dense monotonic sequence, non-decreasing timestamps, intact hashes."""
        from provenance.binding import verify_entity

        stream = await self.events(task_document_id)
        for index, event in enumerate(stream, start=1):
            if event.sequence != index:
                raise LedgerIntegrityError(
                    f"{task_document_id}: sequence gap at position {index} "
                    f"(found {event.sequence})"
                )
            if not verify_entity(event):
                raise LedgerIntegrityError(f"{event.document_id}: content hash does not verify")
        timestamps = [e.recorded_at for e in stream]
        if timestamps != sorted(timestamps):
            raise LedgerIntegrityError(f"{task_document_id}: recorded_at is not monotonic")
        return True

    # -- projection --------------------------------------------------------

    def fold(self, task_document_id: str, stream: list[TaskEvent]) -> TaskProjection:
        """Fold an event stream into the Section 9.2 current-state projection.

        Pure and total: same events in, same projection out. That is what lets a
        projection be rebuilt from the ledger rather than trusted on its own.
        """
        if not stream:
            raise LedgerIntegrityError(f"no events for {task_document_id}; cannot project a state")

        state = TaskState.PROPOSED
        last_state_event = stream[0]
        assigned_alias: str | None = None
        lease_generation: int | None = None
        gate_failures = 0
        rework_count = 0

        times: dict[str, datetime | None] = {
            "queued_at": None,
            "claimed_at": None,
            "started_at": None,
            "last_heartbeat_at": None,
            "candidate_submitted_at": None,
            "verification_started_at": None,
            "blocked_at": None,
            "resumed_at": None,
            "completed_at": None,
            "merged_at": None,
        }
        blocked_seconds = 0.0
        rework_seconds = 0.0
        evaluation_seconds = 0.0
        open_block: datetime | None = None
        open_rework: datetime | None = None
        open_eval: datetime | None = None

        for event in stream:
            when = event.recorded_at
            if event.resulting_state is not None:
                state = event.resulting_state
                last_state_event = event
            if event.event_type in _HEARTBEAT_EVENTS:
                times["last_heartbeat_at"] = when
            if event.lease_generation is not None:
                lease_generation = event.lease_generation
            if event.event_type is TaskEventType.TaskAssigned:
                assigned_alias = str(event.payload.get("assigned_alias", event.actor_alias))
                times["claimed_at"] = times["claimed_at"] or when
            if event.event_type is TaskEventType.LeaseAcquired:
                assigned_alias = assigned_alias or event.actor_alias
                times["claimed_at"] = times["claimed_at"] or when
            if event.event_type in (TaskEventType.TaskCreated, TaskEventType.TaskReady):
                times["queued_at"] = times["queued_at"] or when
            if event.event_type is TaskEventType.WorkerStarted:
                times["started_at"] = times["started_at"] or when
            if event.event_type is TaskEventType.ArtifactSubmitted:
                times["candidate_submitted_at"] = when
                if open_rework is not None:
                    rework_seconds += (when - open_rework).total_seconds()
                    open_rework = None
            if event.event_type is TaskEventType.EvaluationStarted:
                times["verification_started_at"] = when
                open_eval = when
            if event.event_type in (TaskEventType.GatePassed, TaskEventType.GateFailed):
                if open_eval is not None:
                    evaluation_seconds += (when - open_eval).total_seconds()
                    open_eval = None
                if event.event_type is TaskEventType.GateFailed:
                    gate_failures += 1
            if event.event_type is TaskEventType.TaskReworked:
                rework_count += 1
                open_rework = when
            if event.event_type is TaskEventType.TaskBlocked:
                times["blocked_at"] = when
                open_block = when
            elif open_block is not None:
                blocked_seconds += (when - open_block).total_seconds()
                times["resumed_at"] = when
                open_block = None
            if event.event_type is TaskEventType.TaskCompleted:
                times["completed_at"] = when
            if event.event_type is TaskEventType.TaskMerged:
                times["merged_at"] = when

        first_at = stream[0].recorded_at
        last_at = stream[-1].recorded_at
        end_at = times["completed_at"] or last_at

        queue_seconds = _delta(times["queued_at"], times["claimed_at"])
        gross_active = _delta(times["started_at"], end_at)
        active_seconds = None if gross_active is None else max(gross_active - blocked_seconds, 0.0)

        return TaskProjection(
            entity_id=f"PROJ-{task_document_id.split('/', 1)[-1]}",
            envelope=Envelope(
                schema_id="efah.task_projection",
                created_by_alias=self._writer.author_alias,
            ),
            task=task_document_id,
            state=state,
            event_count=len(stream),
            last_event_type=last_state_event.event_type,
            last_event_at=last_at,
            assigned_alias=assigned_alias,
            lease_generation=lease_generation,
            gate_failures=gate_failures,
            rework_count=rework_count,
            queue_seconds=queue_seconds,
            active_seconds=active_seconds,
            blocked_seconds=blocked_seconds or None,
            evaluation_seconds=evaluation_seconds or None,
            rework_seconds=rework_seconds or None,
            total_wall_clock_seconds=(last_at - first_at).total_seconds(),
            **{k: v for k, v in times.items()},
        )

    async def project(self, task_document_id: str) -> TaskProjection:
        """Fold the stored stream. Read-only -- does not persist."""
        return self.fold(task_document_id, await self.events(task_document_id))

    async def persist_projection(self, task_document_id: str) -> tuple[TaskProjection, WriteReceipt]:
        """Recompute and store the projection.

        ``upsert=True``: a projection is derived, so replacing it loses nothing
        the events do not still hold.
        """
        projection = await self.project(task_document_id)
        receipt = await self._writer.write(
            [projection],
            message=f"projection for {task_document_id} at event {projection.event_count}",
            upsert=True,
        )
        return projection, receipt


def _delta(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()
