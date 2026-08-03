"""ORACLE-002 — Lease generation fencing (hierarchy level 1).

Implements ``project-pack/acceptance/oracle-definitions/ORACLE-002-lease-fencing.yaml``
exactly. Contract Sections 9.5, 18.

The failure this kills: a worker whose lease expired while it was thinking
submits over the top of the worker that replaced it. Both submissions look
well-formed. Only the generation number and the recorded input hashes tell
them apart.

The definition's gaming probes are the reason the check list is longer than
"is the generation current":

* GP-001 renewing at submission time must not resurrect a dead generation, so
  superseded generations are remembered with the instant they died.
* GP-002 copying the current generation off the ledger must not be enough, so
  ownership and input hashes are compared too.
* GP-003 the submitter does not get to say when it submitted, so the timestamp
  used is the system-observed one and a self-reported timestamp that disagrees
  is itself a rejection.
* GP-004 force-pushing over the winner must not help, so branch ownership is
  compared against the lease, not against the repository.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from governance.states import TaskState
from oracles.base import Decision, DeterministicOracle, fail, passed, unverifiable

#: Beyond this, an expiry comparison is not trustworthy and the oracle abstains.
DEFAULT_CLOCK_SKEW_TOLERANCE = timedelta(seconds=30)


class LeaseRecord(BaseModel):
    """The authoritative lease as held in TerminusDB (contract Section 9.5)."""

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    lease_id: str
    generation: int
    holder_alias: str
    ownership_mode: str = "exclusive"
    expires_at: datetime
    branch: str
    worktree: str
    input_hashes: dict[str, str] = Field(default_factory=dict)
    #: generation -> instant at which it was superseded or expired (GP-001).
    superseded_generations: dict[int, datetime] = Field(default_factory=dict)


class Submission(BaseModel):
    """A candidate submission arriving at the fence."""

    model_config = ConfigDict(extra="forbid")

    submission_id: str
    work_unit_id: str
    lease_id: str | None = None
    generation: int | None = None
    submitter_alias: str
    branch: str
    worktree: str
    input_hashes: dict[str, str] = Field(default_factory=dict)
    #: What the submitter *claims*. Never used as the decision timestamp (GP-003).
    claimed_submitted_at: datetime | None = None


class FencingSubject(BaseModel):
    """Everything the verdict path needs, resolved by the caller."""

    model_config = ConfigDict(extra="forbid")

    submission: Submission
    #: ``None`` means the lease record was absent from TerminusDB.
    lease: LeaseRecord | None = None
    #: System-event time, not submitter-supplied (contract Section 9.5, GP-003).
    observed_at: datetime | None = None
    clock_skew_observed_seconds: float = 0.0
    clock_skew_tolerance_seconds: float = DEFAULT_CLOCK_SKEW_TOLERANCE.total_seconds()


class LeaseFencingOracle(DeterministicOracle):
    """Level-1 deterministic execution/state oracle."""

    @property
    def oracle_id(self) -> str:
        return "ORACLE-002"

    def decide(self, subject: Any) -> Decision:
        s: FencingSubject = subject
        skew = {"clock_skew_observed": s.clock_skew_observed_seconds}

        # --- unverifiable_when (definition, verbatim) -------------------
        if s.lease is None:
            return unverifiable("lease_record_absent_from_terminusdb", **skew)
        if s.submission.lease_id is None or s.submission.generation is None:
            return unverifiable("submission_carries_no_lease_identifier", **skew)
        if s.observed_at is None:
            return unverifiable("submission_carries_no_lease_identifier", **skew)

        lease = s.lease
        sub = s.submission
        ambiguous_window = abs((lease.expires_at - s.observed_at).total_seconds())
        if (
            s.clock_skew_observed_seconds > s.clock_skew_tolerance_seconds
            and ambiguous_window <= s.clock_skew_observed_seconds
        ):
            return unverifiable(
                "clock_skew_exceeds_tolerance_and_expiry_is_ambiguous", **skew
            )

        reasons: list[str] = []

        if sub.work_unit_id != lease.work_unit_id:
            reasons.append(
                f"submission targets work unit {sub.work_unit_id!r}, lease covers "
                f"{lease.work_unit_id!r}"
            )

        # GP-003: a submitter-supplied timestamp is never authoritative, and a
        # disagreement with the system event is evidence of backdating.
        if sub.claimed_submitted_at is not None and sub.claimed_submitted_at != s.observed_at:
            reasons.append(
                "submitter-supplied timestamp "
                f"{sub.claimed_submitted_at.isoformat()} contradicts the system event at "
                f"{s.observed_at.isoformat()}"
            )

        # reject_as_stale_when, in the definition's order.
        if sub.generation < lease.generation:
            reasons.append(
                f"submission_generation {sub.generation} < current_generation {lease.generation}"
            )
        if lease.expires_at < s.observed_at:
            reasons.append(
                f"lease expired at {lease.expires_at.isoformat()} before the submission was "
                f"observed at {s.observed_at.isoformat()}"
            )
        # GP-001: renewal does not resurrect a generation that already died.
        died_at = lease.superseded_generations.get(sub.generation)
        if died_at is not None and died_at <= s.observed_at:
            reasons.append(
                f"generation {sub.generation} was superseded at {died_at.isoformat()}; "
                "a later renewal does not resurrect it"
            )
        # GP-002 / GP-004: the generation number alone is not sufficient.
        if sub.submitter_alias != lease.holder_alias:
            reasons.append(
                f"branch_or_worktree_owner {sub.submitter_alias!r} != lease_holder "
                f"{lease.holder_alias!r}"
            )
        if sub.branch != lease.branch or sub.worktree != lease.worktree:
            reasons.append(
                f"submission targets {sub.branch}/{sub.worktree}, lease owns "
                f"{lease.branch}/{lease.worktree}"
            )
        if sub.input_hashes != lease.input_hashes:
            reasons.append("input_hashes differ from the lease record")

        if reasons:
            return fail(reasons, TaskState.STALE_ASSIGNMENT, **skew)
        return passed([f"generation {sub.generation} is current and unexpired"], **skew)

    # --- KB-004: concurrent submissions for one work unit ---------------
    def decide_concurrent(self, subjects: list[FencingSubject]) -> list[Decision]:
        """Exactly one PASS across concurrent submissions for the same work unit.

        The definition's KB-004 expects ``exactly_one_PASS_and_one_FAIL``. Two
        submissions can both satisfy the fence when they race inside the same
        generation; the tie is broken deterministically by the system-observed
        instant and then by submission id, never by arrival order at this
        function.
        """
        decisions = [self.decide(s) for s in subjects]
        winners = [
            index
            for index, decision in enumerate(decisions)
            if decision.verdict.value == "PASS"
        ]
        if len(winners) <= 1:
            return decisions

        def sort_key(index: int) -> tuple[str, str]:
            subject = subjects[index]
            observed = subject.observed_at.isoformat() if subject.observed_at else ""
            return (observed, subject.submission.submission_id)

        winners.sort(key=sort_key)
        for loser in winners[1:]:
            decisions[loser] = fail(
                [
                    (
                        "concurrent submission for the same work unit lost the "
                        "deterministic tie-break to "
                        f"{subjects[winners[0]].submission.submission_id}"
                    )
                ],
                TaskState.STALE_ASSIGNMENT,
                clock_skew_observed=subjects[loser].clock_skew_observed_seconds,
            )
        return decisions
