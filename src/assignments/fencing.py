"""ORACLE-002 -- lease generation fencing.

Implements ``project-pack/acceptance/oracle-definitions/ORACLE-002-lease-fencing.yaml``
and the gate that consumes it, GATE-D2-12. Contract Section 9.5:

    A submission from an expired or superseded lease MUST be rejected as stale.

The oracle is deterministic: no model call is in the verdict path
(``model_call_in_verdict_path: false``, ``judge_participates: false``). Every
input to the decision is either a ledger record or the ledger's own clock.

The three-way verdict matters. ``FAIL`` means "this submission is stale, reject
it as ``STALE_ASSIGNMENT``". ``UNVERIFIABLE`` means "the fencing question cannot
be answered from the evidence available" -- absent lease record, no lease
identifier, or a clock-skew window in which expiry is genuinely ambiguous. An
oracle that collapsed ``UNVERIFIABLE`` into ``PASS`` would merge unfenced work;
one that collapsed it into ``FAIL`` would report a fabricated finding. Section
17.2 keeps all three.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assignments.leases import (
    AssignmentLease,
    LeaseEvent,
    LeaseEventType,
    LeaseLedger,
    ManualClock,
)
from governance.envelope import content_hash
from governance.states import TaskState, Verdict

ORACLE_ID = "ORACLE-002"
ORACLE_VERSION = "1.0.0"

#: Section 9.8: time comes from system events. A submitter-declared timestamp
#: further than this from the observed time is skew, and skew that lands on the
#: expiry boundary makes the verdict UNVERIFIABLE rather than a coin flip.
DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


class Submission(BaseModel):
    """What a worker hands back. Section 9.4 / Section 9.5 inputs to fencing.

    ``claimed_submitted_at`` is recorded and *never* used for the expiry
    decision -- ORACLE-002 GP-003 is precisely an attempt to backdate it.
    """

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    lease_id: str | None = None
    lease_generation: int | None = None

    branch: str = ""
    worktree: str = ""
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_schema: str = ""
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    submitted_by_alias: str = ""

    #: Advisory only. Kept for the transcript, excluded from the decision.
    claimed_submitted_at: datetime | None = None


class FencingVerdict(BaseModel):
    """ORACLE-002 result plus the health block it must emit every time."""

    model_config = ConfigDict(extra="forbid")

    oracle_id: str = ORACLE_ID
    verdict: Verdict
    resulting_task_state: TaskState | None = None
    reasons: list[str] = Field(default_factory=list)
    observed_at: datetime
    lease_generation_submitted: int | None = None
    lease_generation_current: int | None = None
    health: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_stale(self) -> bool:
        return self.resulting_task_state is TaskState.STALE_ASSIGNMENT


class LeaseFencingOracle:
    """Deterministic checker for ORACLE-002.

    Ordering is intentional: unanswerable questions are settled before stale
    ones, so a missing lease record can never be reported as a stale submission.
    """

    oracle_id = ORACLE_ID
    oracle_version = ORACLE_VERSION

    def __init__(
        self,
        ledger: LeaseLedger,
        *,
        clock_skew_tolerance_seconds: float = DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
        last_audit_date: str = "2026-08-02",
    ) -> None:
        self.ledger = ledger
        self.clock_skew_tolerance_seconds = clock_skew_tolerance_seconds
        self.last_audit_date = last_audit_date
        self._fixture_suite_result: str | None = None

    # -- verdict path ------------------------------------------------------

    def evaluate(self, submission: Submission) -> FencingVerdict:
        observed_at = self.ledger.clock.now()
        skew = self._observed_skew(submission, observed_at)

        # 1. unverifiable_when: submission_carries_no_lease_identifier
        if not submission.lease_id or submission.lease_generation is None:
            return self._verdict(
                Verdict.UNVERIFIABLE,
                None,
                ["submission_carries_no_lease_identifier"],
                observed_at,
                skew,
                submission,
                None,
            )

        # 2. unverifiable_when: lease_record_absent_from_terminusdb
        current = self.ledger.current_for_work_unit(submission.work_unit_id)
        claimed = self.ledger.get(submission.lease_id)
        if current is None or claimed is None:
            return self._verdict(
                Verdict.UNVERIFIABLE,
                None,
                ["lease_record_absent"],
                observed_at,
                skew,
                submission,
                current,
            )

        # 3. unverifiable_when: clock_skew_exceeds_tolerance_and_expiry_is_ambiguous
        boundary_distance = abs((observed_at - claimed.expires_at).total_seconds())
        if skew > self.clock_skew_tolerance_seconds and boundary_distance <= self.clock_skew_tolerance_seconds:
            return self._verdict(
                Verdict.UNVERIFIABLE,
                None,
                ["clock_skew_exceeds_tolerance_and_expiry_is_ambiguous"],
                observed_at,
                skew,
                submission,
                current,
            )

        reasons = self._stale_reasons(submission, claimed, current, observed_at)
        if reasons:
            return self._verdict(
                Verdict.FAIL,
                TaskState.STALE_ASSIGNMENT,
                reasons,
                observed_at,
                skew,
                submission,
                current,
            )
        return self._verdict(Verdict.PASS, None, [], observed_at, skew, submission, current)

    def _stale_reasons(
        self,
        submission: Submission,
        claimed: AssignmentLease,
        current: AssignmentLease,
        observed_at: datetime,
    ) -> list[str]:
        """``reject_as_stale_when`` from the oracle definition, all of it.

        Every condition is evaluated -- not short-circuited -- so the transcript
        shows *all* the ways a submission was stale. GP-002 copies the current
        generation number from the ledger; it still fails here on ownership and
        input hashes.
        """
        reasons: list[str] = []

        if submission.lease_generation is not None and submission.lease_generation < current.generation:
            reasons.append("submission_generation_below_current_generation")
        if claimed.lease_id != current.lease_id:
            reasons.append("lease_id_is_not_the_current_holder")
        if claimed.superseded_at is not None:
            reasons.append("lease_superseded")
        if claimed.released_at is not None:
            reasons.append("lease_released")
        if submission.lease_generation is not None and submission.lease_generation != claimed.generation:
            reasons.append("submission_generation_does_not_match_its_own_lease_record")
        if claimed.is_expired_at(observed_at):
            reasons.append("lease_expired_before_submission_timestamp")

        branch_owner = self.ledger.owner_of_branch(submission.branch) if submission.branch else None
        worktree_owner = self.ledger.owner_of_worktree(submission.worktree) if submission.worktree else None
        if submission.branch and submission.branch != claimed.branch:
            reasons.append("branch_not_owned_by_submitting_lease")
        elif submission.branch and branch_owner is not None and branch_owner.lease_id != claimed.lease_id:
            reasons.append("branch_owner_is_a_different_lease")
        if submission.worktree and submission.worktree != claimed.worktree:
            reasons.append("worktree_not_owned_by_submitting_lease")
        elif submission.worktree and worktree_owner is not None and worktree_owner.lease_id != claimed.lease_id:
            reasons.append("worktree_owner_is_a_different_lease")

        if dict(submission.input_hashes) != dict(claimed.input_hashes):
            reasons.append("input_hashes_differ_from_lease_record")

        if (
            claimed.permitted_output_schemas
            and submission.output_schema
            and submission.output_schema not in claimed.permitted_output_schemas
        ):
            reasons.append("output_schema_not_permitted_by_lease")

        return reasons

    # -- health ------------------------------------------------------------

    def _verdict(
        self,
        verdict: Verdict,
        task_state: TaskState | None,
        reasons: list[str],
        observed_at: datetime,
        skew: float,
        submission: Submission,
        current: AssignmentLease | None,
    ) -> FencingVerdict:
        body = {
            "oracle_id": ORACLE_ID,
            "oracle_version": ORACLE_VERSION,
            "verdict": str(verdict),
            "reasons": reasons,
            "work_unit_id": submission.work_unit_id,
            "lease_id": submission.lease_id,
            "lease_generation": submission.lease_generation,
            "observed_at": observed_at.isoformat(),
        }
        return FencingVerdict(
            verdict=verdict,
            resulting_task_state=task_state,
            reasons=reasons,
            observed_at=observed_at,
            lease_generation_submitted=submission.lease_generation,
            lease_generation_current=current.generation if current else None,
            health={
                "oracle_version": ORACLE_VERSION,
                "content_hash": content_hash(body),
                "fixture_suite_result": self.fixture_suite_result(),
                "last_audit_date": self.last_audit_date,
                "clock_skew_observed": skew,
            },
        )

    def _observed_skew(self, submission: Submission, observed_at: datetime) -> float:
        if submission.claimed_submitted_at is None:
            return 0.0
        claimed = submission.claimed_submitted_at
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=UTC)
        return abs((observed_at - claimed).total_seconds())

    def fixture_suite_result(self) -> str:
        """ORACLE-002 ``health_emitted_with_every_result -> fixture_suite_result``.

        Computed once per oracle instance by running the definition's own
        known-good and known-bad fixtures against a throwaway ledger. An oracle
        that cannot still kill its own known-bad cases has no business emitting
        a verdict, and this is the only way the caller finds that out.
        """
        if self._fixture_suite_result is None:
            self._fixture_suite_result = run_fixture_suite()
        return self._fixture_suite_result


class SubmissionGateway:
    """Fencing runs *before* anything touches a branch.

    GATE-D2-12 A4: "A rejected stale submission does not corrupt the winning
    worker's branch." The applier is only ever invoked on ``PASS``; there is no
    path from a ``FAIL`` or ``UNVERIFIABLE`` verdict to a write.
    """

    def __init__(
        self,
        oracle: LeaseFencingOracle,
        applier: Callable[[Submission], Any] | None = None,
    ) -> None:
        self.oracle = oracle
        self.applier = applier

    def submit(self, submission: Submission) -> FencingVerdict:
        ledger = self.oracle.ledger
        verdict = self.oracle.evaluate(submission)
        ledger.record(
            LeaseEvent(
                event=LeaseEventType.SUBMISSION_OBSERVED,
                at=verdict.observed_at,
                work_unit_id=submission.work_unit_id,
                lease_id=submission.lease_id or "",
                generation=submission.lease_generation or 0,
                detail={"verdict": str(verdict.verdict), "reasons": verdict.reasons},
            )
        )
        if verdict.verdict is not Verdict.PASS:
            ledger.record(
                LeaseEvent(
                    event=LeaseEventType.SUBMISSION_REJECTED,
                    at=verdict.observed_at,
                    work_unit_id=submission.work_unit_id,
                    lease_id=submission.lease_id or "",
                    generation=submission.lease_generation or 0,
                    detail={
                        "resulting_task_state": str(verdict.resulting_task_state or ""),
                        "reasons": verdict.reasons,
                    },
                )
            )
            return verdict

        if self.applier is not None:
            self.applier(submission)
        ledger.record(
            LeaseEvent(
                event=LeaseEventType.SUBMISSION_ACCEPTED,
                at=verdict.observed_at,
                work_unit_id=submission.work_unit_id,
                lease_id=submission.lease_id or "",
                generation=submission.lease_generation or 0,
                detail={"artifact_hashes": submission.artifact_hashes},
            )
        )
        return verdict


# ---------------------------------------------------------------------------
# ORACLE-002 fixture suite (known_good, known_bad, gaming_probes)
# ---------------------------------------------------------------------------


def _fixture_ledger() -> tuple[Any, ManualClock]:
    from assignments.leases import InMemoryLeaseLedger  # local: avoids import cycle at module import

    clock = ManualClock()
    return InMemoryLeaseLedger(clock=clock), clock


def _acquire(ledger: Any, **overrides: Any) -> AssignmentLease:
    params: dict[str, Any] = {
        "work_unit_id": "WU-0042",
        "role": "implementer",
        "blinded_alias": "MODEL-A",
        "branch": "feat/wu-0042",
        "worktree": "/wt/wu-0042",
        "input_hashes": {"spec": "sha256:aaa"},
        "permitted_output_schemas": ("efah.artifact",),
    }
    params.update(overrides)
    return ledger.acquire(**params)


def _submission(lease: AssignmentLease, **overrides: Any) -> Submission:
    params: dict[str, Any] = {
        "work_unit_id": lease.work_unit_id,
        "lease_id": lease.lease_id,
        "lease_generation": lease.generation,
        "branch": lease.branch,
        "worktree": lease.worktree,
        "input_hashes": dict(lease.input_hashes),
        "output_schema": "efah.artifact",
        "submitted_by_alias": lease.blinded_alias,
    }
    params.update(overrides)
    return Submission(**params)


def _fixture_cases() -> Iterable[tuple[str, Verdict]]:
    """Each fixture from the ORACLE-002 definition, as an executable case."""

    # KG-001 -- current generation, unexpired lease, matching worktree.
    ledger, _clock = _fixture_ledger()
    lease = _acquire(ledger)
    yield "KG-001", LeaseFencingOracle(ledger).evaluate(_submission(lease)).verdict

    # KB-001 -- worker submits after its lease expired and was reassigned.
    ledger, clock = _fixture_ledger()
    lease = _acquire(ledger)
    clock.advance(lease.renewal_policy.lease_duration_seconds + 1)
    _acquire(ledger)  # reassigned to a new generation
    yield "KB-001", LeaseFencingOracle(ledger).evaluate(_submission(lease)).verdict

    # KB-002 -- worker holds generation 3 while current generation is 4.
    ledger, clock = _fixture_ledger()
    first = _acquire(ledger)
    clock.advance(1)
    _acquire(ledger, supersede=True)
    yield "KB-002", LeaseFencingOracle(ledger).evaluate(_submission(first)).verdict

    # KB-003 -- submission targets a branch owned by a different lease.
    ledger, _clock = _fixture_ledger()
    mine = _acquire(ledger)
    theirs = _acquire(ledger, work_unit_id="WU-0043", branch="feat/wu-0043", worktree="/wt/wu-0043")
    yield "KB-003", LeaseFencingOracle(ledger).evaluate(
        _submission(mine, branch=theirs.branch, worktree=theirs.worktree)
    ).verdict

    # KB-004 -- two workers submit concurrently; exactly one PASS.
    ledger, clock = _fixture_ledger()
    worker_a = _acquire(ledger)
    clock.advance(1)
    worker_b = _acquire(ledger, supersede=True, blinded_alias="MODEL-B")
    oracle = LeaseFencingOracle(ledger)
    verdicts = [oracle.evaluate(_submission(worker_a)).verdict, oracle.evaluate(_submission(worker_b)).verdict]
    yield "KB-004", Verdict.PASS if verdicts.count(Verdict.PASS) == 1 else Verdict.FAIL

    # GP-001 -- renew at submission time to look current.
    ledger, clock = _fixture_ledger()
    lease = _acquire(ledger)
    clock.advance(lease.renewal_policy.lease_duration_seconds + 1)
    with contextlib.suppress(Exception):
        ledger.renew(lease.lease_id)
    yield "GP-001", LeaseFencingOracle(ledger).evaluate(_submission(lease)).verdict

    # GP-002 -- copy the current generation from the ledger, wrong inputs.
    ledger, clock = _fixture_ledger()
    first = _acquire(ledger)
    clock.advance(1)
    current = _acquire(ledger, supersede=True, input_hashes={"spec": "sha256:bbb"})
    yield "GP-002", LeaseFencingOracle(ledger).evaluate(
        _submission(first, lease_generation=current.generation)
    ).verdict

    # GP-003 -- backdate the submission timestamp to before expiry.
    ledger, clock = _fixture_ledger()
    lease = _acquire(ledger)
    clock.advance(lease.renewal_policy.lease_duration_seconds + 600)
    yield "GP-003", LeaseFencingOracle(ledger).evaluate(
        _submission(lease, claimed_submitted_at=lease.acquired_at)
    ).verdict

    # GP-004 -- stale worker force-pushes over the current branch.
    ledger, clock = _fixture_ledger()
    stale = _acquire(ledger)
    clock.advance(1)
    winner = _acquire(ledger, supersede=True, blinded_alias="MODEL-B", branch="feat/wu-0042-v2")
    yield "GP-004", LeaseFencingOracle(ledger).evaluate(_submission(stale, branch=winner.branch)).verdict


_EXPECTED_FIXTURES: dict[str, Verdict] = {
    "KG-001": Verdict.PASS,
    "KB-001": Verdict.FAIL,
    "KB-002": Verdict.FAIL,
    "KB-003": Verdict.FAIL,
    "KB-004": Verdict.PASS,  # exactly one PASS among the concurrent pair
    "GP-001": Verdict.FAIL,
    "GP-002": Verdict.FAIL,
    "GP-003": Verdict.FAIL,
    "GP-004": Verdict.FAIL,
}


#: The fixture suite instantiates real oracles, and every real oracle emits a
#: health block containing the fixture-suite result. Without this guard the
#: health block would recurse into the suite that produces it. Re-entrant calls
#: report ``IN_PROGRESS``; the outer call caches the real answer.
_SUITE_RUNNING = False
_SUITE_CACHE: str | None = None


def fixture_report() -> dict[str, dict[str, str]]:
    """Run every ORACLE-002 fixture and report actual against expected."""
    global _SUITE_RUNNING
    outermost = not _SUITE_RUNNING
    _SUITE_RUNNING = True
    try:
        report: dict[str, dict[str, str]] = {}
        for fixture_id, actual in _fixture_cases():
            expected = _EXPECTED_FIXTURES[fixture_id]
            report[fixture_id] = {
                "expected": str(expected),
                "actual": str(actual),
                "result": "PASS" if actual is expected else "FAIL",
            }
        return report
    finally:
        if outermost:
            _SUITE_RUNNING = False


def run_fixture_suite(*, force: bool = False) -> str:
    """Cached suite verdict, safe to call from inside a verdict's health block."""
    global _SUITE_CACHE
    if _SUITE_RUNNING:
        return "IN_PROGRESS"
    if _SUITE_CACHE is not None and not force:
        return _SUITE_CACHE
    report = fixture_report()
    failed = sorted(k for k, v in report.items() if v["result"] != "PASS")
    _SUITE_CACHE = f"FAIL:{','.join(failed)}" if failed else f"PASS:{len(report)}/{len(_EXPECTED_FIXTURES)}"
    return _SUITE_CACHE
