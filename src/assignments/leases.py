"""Assignment leases with generation fencing -- Contract Section 9.5.

    Every active work unit MUST have: assigned role and blinded alias;
    exclusive/shared ownership mode; lease ID and generation; lease expiry and
    renewal policy; repository branch/worktree ownership; input hashes;
    permitted output schemas. A submission from an expired or superseded lease
    MUST be rejected as stale.

Durations come from ``autonomy-policy.yaml`` (1800s lease, 300s heartbeat), not
from a constant invented here.

Two design points are load-bearing rather than stylistic:

* **Time is injected, never read ad hoc.** Contract Section 9.8: "Time MUST be
  measured from system events, not agent estimates." Every timestamp in this
  module comes from the ledger's :class:`Clock`. A submitter cannot influence
  it, which is what defeats ORACLE-002 gaming probe GP-003 (backdating).
* **Renewal cannot resurrect an expired lease.** GP-001 tries exactly that.
  :meth:`InMemoryLeaseLedger.renew` raises instead, so there is no code path
  where a dead worker heartbeats itself back into ownership.

Storage: Section 9.5 records live in TerminusDB, which WS-B owns. This module
defines the :class:`LeaseLedger` protocol that the TerminusDB-backed ledger must
satisfy, and ships a complete in-process implementation the runtime uses today.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

#: ``autonomy-policy.yaml -> assignment_policy``.
LEASE_DURATION_SECONDS = 1800
LEASE_RENEWAL_HEARTBEAT_SECONDS = 300


class OwnershipMode(StrEnum):
    """Section 9.5 "exclusive/shared ownership mode"."""

    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"


class LeaseEventType(StrEnum):
    """Section 9.2 task-ledger events relevant to assignment."""

    LEASE_ACQUIRED = "LeaseAcquired"
    LEASE_RENEWED = "LeaseRenewed"
    LEASE_SUPERSEDED = "LeaseSuperseded"
    LEASE_EXPIRED = "LeaseExpired"
    LEASE_RELEASED = "LeaseReleased"
    SUBMISSION_OBSERVED = "SubmissionObserved"
    SUBMISSION_REJECTED = "SubmissionRejected"
    SUBMISSION_ACCEPTED = "SubmissionAccepted"


class LeaseError(RuntimeError):
    """Base for lease-ledger refusals."""


class LeaseExpiredError(LeaseError):
    """GP-001: an expired lease cannot be renewed back into validity."""


class LeaseSupersededError(LeaseError):
    """A newer generation owns this work unit."""


class WorkUnitAlreadyLeased(LeaseError):
    """An exclusive, live lease already covers this work unit."""


class OwnershipConflict(LeaseError):
    """Section 9.5 branch/worktree ownership is exclusive per live lease."""


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Wall clock, UTC. The only clock a production ledger should be given."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class ManualClock:
    """Deterministic clock for lease-expiry probes.

    Real infrastructure, not a mock of the ledger: the ledger under test is the
    real one. Only the passage of time is controlled, because a 1800-second
    lease cannot otherwise be expired inside a test.
    """

    current: datetime = field(default_factory=lambda: datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current


class RenewalPolicy(BaseModel):
    """Section 9.5 "lease expiry and renewal policy"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_duration_seconds: int = LEASE_DURATION_SECONDS
    heartbeat_seconds: int = LEASE_RENEWAL_HEARTBEAT_SECONDS

    @property
    def heartbeats_per_lease(self) -> int:
        return self.lease_duration_seconds // self.heartbeat_seconds


class AssignmentLease(BaseModel):
    """Every field Section 9.5 makes mandatory for an active work unit."""

    model_config = ConfigDict(extra="forbid")

    lease_id: str
    generation: int
    work_unit_id: str

    role: str
    blinded_alias: str
    ownership_mode: OwnershipMode

    branch: str
    worktree: str

    input_hashes: dict[str, str] = Field(default_factory=dict)
    permitted_output_schemas: tuple[str, ...] = ()

    renewal_policy: RenewalPolicy = RenewalPolicy()

    acquired_at: datetime
    expires_at: datetime
    last_heartbeat_at: datetime
    released_at: datetime | None = None
    superseded_at: datetime | None = None

    def is_expired_at(self, moment: datetime) -> bool:
        return moment > self.expires_at

    def is_live_at(self, moment: datetime) -> bool:
        return self.released_at is None and self.superseded_at is None and not self.is_expired_at(moment)

    def heartbeat_overdue_at(self, moment: datetime) -> bool:
        overdue = self.last_heartbeat_at + timedelta(seconds=self.renewal_policy.heartbeat_seconds)
        return moment > overdue


class LeaseEvent(BaseModel):
    """Append-only assignment event. Section 9.2, Section 9.8."""

    model_config = ConfigDict(extra="forbid")

    event: LeaseEventType
    at: datetime
    work_unit_id: str
    lease_id: str
    generation: int
    detail: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LeaseLedger(Protocol):
    """The seam WS-B's TerminusDB-backed ledger must satisfy.

    ORACLE-002's method reads "read_current_lease_record_from_terminusdb"; the
    fencing oracle is written against this protocol so swapping the store does
    not change the fencing logic.
    """

    clock: Clock

    def acquire(self, **kwargs: Any) -> AssignmentLease: ...
    def renew(self, lease_id: str) -> AssignmentLease: ...
    def release(self, lease_id: str) -> AssignmentLease: ...
    def get(self, lease_id: str) -> AssignmentLease | None: ...
    def current_for_work_unit(self, work_unit_id: str) -> AssignmentLease | None: ...
    def owner_of_worktree(self, worktree: str) -> AssignmentLease | None: ...
    def owner_of_branch(self, branch: str) -> AssignmentLease | None: ...
    def record(self, event: LeaseEvent) -> None: ...
    def events(self) -> Iterable[LeaseEvent]: ...


class InMemoryLeaseLedger:
    """Complete in-process implementation of :class:`LeaseLedger`.

    Generation numbering is per work unit and strictly increasing. Superseding
    is the *only* way to take a live exclusive work unit, and it stamps the
    displaced lease with ``superseded_at`` -- which is what makes a later
    submission from that lease detectably stale rather than merely late.
    """

    def __init__(self, clock: Clock | None = None, policy: RenewalPolicy | None = None) -> None:
        self.clock: Clock = clock or SystemClock()
        self.policy = policy or RenewalPolicy()
        self._leases: dict[str, AssignmentLease] = {}
        self._by_work_unit: dict[str, list[str]] = {}
        self._events: list[LeaseEvent] = []

    # -- write side --------------------------------------------------------

    def acquire(
        self,
        *,
        work_unit_id: str,
        role: str,
        blinded_alias: str,
        branch: str,
        worktree: str,
        input_hashes: dict[str, str] | None = None,
        permitted_output_schemas: Iterable[str] = (),
        ownership_mode: OwnershipMode = OwnershipMode.EXCLUSIVE,
        supersede: bool = False,
        renewal_policy: RenewalPolicy | None = None,
    ) -> AssignmentLease:
        now = self.clock.now()
        policy = renewal_policy or self.policy

        incumbent = self.current_for_work_unit(work_unit_id)
        if incumbent is not None and incumbent.is_live_at(now):
            if incumbent.ownership_mode is OwnershipMode.EXCLUSIVE and not supersede:
                raise WorkUnitAlreadyLeased(
                    f"{work_unit_id} is held by {incumbent.lease_id} generation {incumbent.generation} "
                    f"until {incumbent.expires_at.isoformat()}"
                )

        for other in self._live_leases(now):
            if other.work_unit_id == work_unit_id:
                continue
            if other.worktree == worktree:
                raise OwnershipConflict(f"worktree {worktree!r} already owned by lease {other.lease_id}")
            if other.branch == branch:
                raise OwnershipConflict(f"branch {branch!r} already owned by lease {other.lease_id}")

        generation = (incumbent.generation + 1) if incumbent is not None else 1
        if incumbent is not None:
            self._supersede(incumbent, now, superseded_by_generation=generation)

        lease = AssignmentLease(
            lease_id=f"LEASE-{uuid.uuid4().hex[:12]}",
            generation=generation,
            work_unit_id=work_unit_id,
            role=role,
            blinded_alias=blinded_alias,
            ownership_mode=ownership_mode,
            branch=branch,
            worktree=worktree,
            input_hashes=dict(input_hashes or {}),
            permitted_output_schemas=tuple(permitted_output_schemas),
            renewal_policy=policy,
            acquired_at=now,
            expires_at=now + timedelta(seconds=policy.lease_duration_seconds),
            last_heartbeat_at=now,
        )
        self._leases[lease.lease_id] = lease
        self._by_work_unit.setdefault(work_unit_id, []).append(lease.lease_id)
        self.record(
            LeaseEvent(
                event=LeaseEventType.LEASE_ACQUIRED,
                at=now,
                work_unit_id=work_unit_id,
                lease_id=lease.lease_id,
                generation=generation,
                detail={"role": role, "blinded_alias": blinded_alias, "worktree": worktree, "branch": branch},
            )
        )
        return lease

    def renew(self, lease_id: str) -> AssignmentLease:
        """Extend a live lease. Refuses expired and superseded leases.

        ORACLE-002 GP-001 attempts renewal at submission time to make a dead
        lease look current. There is no branch here that allows it.
        """
        now = self.clock.now()
        lease = self._require(lease_id)
        if lease.superseded_at is not None:
            raise LeaseSupersededError(f"{lease_id} was superseded at {lease.superseded_at.isoformat()}")
        if lease.released_at is not None:
            raise LeaseError(f"{lease_id} was released at {lease.released_at.isoformat()}")
        if lease.is_expired_at(now):
            self.record(
                LeaseEvent(
                    event=LeaseEventType.LEASE_EXPIRED,
                    at=now,
                    work_unit_id=lease.work_unit_id,
                    lease_id=lease_id,
                    generation=lease.generation,
                    detail={"expired_at": lease.expires_at.isoformat(), "renewal_refused": True},
                )
            )
            raise LeaseExpiredError(
                f"{lease_id} expired at {lease.expires_at.isoformat()}; renewal does not resurrect a generation"
            )

        renewed = lease.model_copy(
            update={
                "expires_at": now + timedelta(seconds=lease.renewal_policy.lease_duration_seconds),
                "last_heartbeat_at": now,
            }
        )
        self._leases[lease_id] = renewed
        self.record(
            LeaseEvent(
                event=LeaseEventType.LEASE_RENEWED,
                at=now,
                work_unit_id=lease.work_unit_id,
                lease_id=lease_id,
                generation=lease.generation,
                detail={"expires_at": renewed.expires_at.isoformat()},
            )
        )
        return renewed

    #: Section 9.5 heartbeat is a renewal; named separately because the
    #: autonomy policy sets a heartbeat interval distinct from lease duration.
    heartbeat = renew

    def release(self, lease_id: str) -> AssignmentLease:
        now = self.clock.now()
        lease = self._require(lease_id)
        released = lease.model_copy(update={"released_at": now})
        self._leases[lease_id] = released
        self.record(
            LeaseEvent(
                event=LeaseEventType.LEASE_RELEASED,
                at=now,
                work_unit_id=lease.work_unit_id,
                lease_id=lease_id,
                generation=lease.generation,
            )
        )
        return released

    def record(self, event: LeaseEvent) -> None:
        self._events.append(event)

    # -- read side ---------------------------------------------------------

    def get(self, lease_id: str) -> AssignmentLease | None:
        return self._leases.get(lease_id)

    def current_for_work_unit(self, work_unit_id: str) -> AssignmentLease | None:
        """Highest generation on record, live or not.

        Returning the expired incumbent rather than ``None`` is deliberate: the
        fencing oracle must be able to distinguish "expired" (FAIL, stale) from
        "no record at all" (UNVERIFIABLE).
        """
        ids = self._by_work_unit.get(work_unit_id)
        if not ids:
            return None
        return max((self._leases[i] for i in ids), key=lambda lease: lease.generation)

    def owner_of_worktree(self, worktree: str) -> AssignmentLease | None:
        now = self.clock.now()
        for lease in self._live_leases(now):
            if lease.worktree == worktree:
                return lease
        return None

    def owner_of_branch(self, branch: str) -> AssignmentLease | None:
        now = self.clock.now()
        for lease in self._live_leases(now):
            if lease.branch == branch:
                return lease
        return None

    def events(self) -> list[LeaseEvent]:
        return list(self._events)

    def active_leases(self) -> list[AssignmentLease]:
        return list(self._live_leases(self.clock.now()))

    # -- internals ---------------------------------------------------------

    def _live_leases(self, moment: datetime) -> Iterator[AssignmentLease]:
        for lease in self._leases.values():
            if lease.is_live_at(moment):
                yield lease

    def _require(self, lease_id: str) -> AssignmentLease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseError(f"no lease record for {lease_id}")
        return lease

    def _supersede(self, lease: AssignmentLease, now: datetime, *, superseded_by_generation: int) -> None:
        self._leases[lease.lease_id] = lease.model_copy(update={"superseded_at": now})
        self.record(
            LeaseEvent(
                event=LeaseEventType.LEASE_SUPERSEDED,
                at=now,
                work_unit_id=lease.work_unit_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
                detail={"superseded_by_generation": superseded_by_generation},
            )
        )
