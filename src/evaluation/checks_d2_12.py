"""GATE-D2-12 — leases, worktrees, and stale-worker rejection.

Contract Section 9.5 · ORACLE-002. The gate's four assertions are executed here
against the real assignment ledger (:mod:`assignments.leases`), the real
submission fence (:mod:`assignments.fencing`) and the minted ORACLE-002 the gate
names (``ctx.oracles["ORACLE-002"]``). Nothing here mocks the subject: the only
injected component is :class:`~assignments.leases.ManualClock`, because an
1800-second lease cannot otherwise be expired inside a check.

This lives outside :mod:`evaluation.checks` because it is a self-contained set
with its own probe machinery; :data:`CHECKS_D2_12` is what the registry merges.

Two honesty constraints run through every check, because getting either wrong
would produce a green that measured something adjacent to the assertion:

* **A worktree here is an ownership identifier, not a directory.** Section 9.5
  makes "repository branch/worktree ownership" a *record on the lease*, and the
  ledger enforces that at most one live lease holds a given worktree or branch
  string. That is what A1 proves. It does not prove two agents received
  isolated checkouts on disk, and the evidence says so rather than letting a
  reader infer filesystem isolation from the word "worktree".
* **Branch state in A4 is modelled by the applier callback.**
  ``SubmissionGateway`` takes a ``Callable[[Submission], Any]`` and invokes it
  only on ``PASS``. The probe substitutes a recording spy for that callback, so
  what A4 proves is that *no write path is reachable from a rejected verdict* --
  the fence is evaluated before the applier is ever called. A real git applier
  would occupy exactly that callback, but this check writes no git branch, and
  the evidence records the limit instead of implying more.

Every check carries a negative control, because each of these properties is
trivially satisfiable by a broken implementation: a fence that rejects
everything "rejects stale submissions", and an applier that is never called
"leaves the winning branch unmodified". So each check also exercises the arm
that must *not* fire -- a live lease's submission is accepted, and applied --
and fails if that arm misbehaves. A check that cannot fail is not a check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assignments.fencing import LeaseFencingOracle as LedgerFencingOracle
from assignments.fencing import Submission as LedgerSubmission
from assignments.fencing import SubmissionGateway
from assignments.leases import (
    AssignmentLease,
    InMemoryLeaseLedger,
    LeaseError,
    LeaseEventType,
    ManualClock,
    OwnershipConflict,
    WorkUnitAlreadyLeased,
)
from evaluation.gate_spec import AssertionSpec, GateSpec
from governance.envelope import content_hash
from governance.states import TaskState, Verdict
from oracles import fixtures as fx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular — and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; ``ok`` and ``bad`` are the only runtime needs, and resolving them
# on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)

#: Fence reasons referenced by name rather than by prose, so a reworded reason
#: string surfaces as a failing check instead of a silently weakened one.
_REASON_EXPIRED = "lease_expired_before_submission_timestamp"
_REASON_GENERATION_BELOW_CURRENT = "submission_generation_below_current_generation"
_REASON_SUPERSEDED = "lease_superseded"


# ---------------------------------------------------------------------------
# Probe construction — a real ledger per probe, with a controlled clock
# ---------------------------------------------------------------------------

def _ledger() -> tuple[InMemoryLeaseLedger, ManualClock]:
    clock = ManualClock()
    return InMemoryLeaseLedger(clock=clock), clock


def _acquire(ledger: InMemoryLeaseLedger, **overrides: Any) -> AssignmentLease:
    params: dict[str, Any] = {
        "work_unit_id": "WU-D2-12",
        "role": "implementer",
        "blinded_alias": "MODEL-A",
        "branch": "feat/wu-d2-12",
        "worktree": "/wt/wu-d2-12",
        "input_hashes": {"work_unit": "sha256:aaa"},
        "permitted_output_schemas": ("efah.work_unit_candidate",),
    }
    params.update(overrides)
    return ledger.acquire(**params)


def _submission(lease: AssignmentLease, **overrides: Any) -> LedgerSubmission:
    """A well-formed submission from ``lease``.

    Every field a submitter controls is copied from its own lease record, which
    is exactly what a stale worker would honestly send: it does not know it
    lost. The fence therefore has to decide from ledger state, not from a
    malformed payload it could reject for some unrelated reason.
    """
    params: dict[str, Any] = {
        "work_unit_id": lease.work_unit_id,
        "lease_id": lease.lease_id,
        "lease_generation": lease.generation,
        "branch": lease.branch,
        "worktree": lease.worktree,
        "input_hashes": dict(lease.input_hashes),
        "output_schema": "efah.work_unit_candidate",
        "submitted_by_alias": lease.blinded_alias,
    }
    params.update(overrides)
    return LedgerSubmission(**params)


def _lease_record(lease: AssignmentLease) -> dict[str, Any]:
    return {
        "lease_id": lease.lease_id,
        "generation": lease.generation,
        "work_unit_id": lease.work_unit_id,
        "blinded_alias": lease.blinded_alias,
        "ownership_mode": lease.ownership_mode.value,
        "branch": lease.branch,
        "worktree": lease.worktree,
        "acquired_at": lease.acquired_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
    }


def _verdict_record(probe: str, verdict: Any) -> dict[str, Any]:
    return {
        "probe": probe,
        "verdict": verdict.verdict.value,
        "resulting_task_state": (
            verdict.resulting_task_state.value if verdict.resulting_task_state else None
        ),
        "reasons": list(verdict.reasons),
        "observed_at": verdict.observed_at.isoformat(),
        "lease_generation_submitted": verdict.lease_generation_submitted,
        "lease_generation_current": verdict.lease_generation_current,
    }


def _minted_oracle_decision(ctx: GateContext, fixture_id: str) -> dict[str, Any]:
    """Decide one ORACLE-002 pack fixture with the minted oracle.

    The ledger probes exercise the runtime fence. This exercises the oracle the
    gate's ``oracle_type`` points at -- ORACLE-002, constructed from its pack
    definition and minted record -- against the pack's own fixture for the same
    condition. Agreement between the two is what makes the runtime verdict
    attributable to the contract rather than to this file's opinion of it.
    """
    oracle = ctx.oracles["ORACLE-002"]
    fixture = next(f for f in fx.fixtures_for("ORACLE-002") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    return {
        "fixture_id": fixture.fixture_id,
        "kind": fixture.kind,
        "description": fixture.description,
        "expected_verdict": fixture.expected_verdict.value,
        "observed_verdict": decision.verdict.value,
        "failure_state": decision.failure_state.value if decision.failure_state else None,
        "reasons": decision.reasons,
    }


def _is_stale(verdict: Any) -> bool:
    return (
        verdict.verdict is Verdict.FAIL
        and verdict.resulting_task_state is TaskState.STALE_ASSIGNMENT
    )


def _event_counts(ledger: InMemoryLeaseLedger) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in ledger.events():
        counts[event.event.value] = counts.get(event.event.value, 0) + 1
    return counts


# ===========================================================================
# A1 — two parallel work units hold distinct leases and distinct worktrees
# ===========================================================================

def d2_12_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``concurrent_assignment_probe`` → ``distinct_lease_ids and distinct_worktrees``.

    Distinctness alone is a weak claim: two ``uuid4`` values differ by
    construction, and a probe that passes two different worktree strings gets
    two different worktrees whatever the ledger does. So this proves the
    property the assertion is about. At one observed instant both work units are
    *simultaneously* leased -- overlapping liveness is what "parallel" means for
    a lease -- each identifier resolves to exactly one owning lease, and a third
    acquisition attempting an already-owned worktree or branch is refused.
    Without that last arm, "distinct worktrees" would describe this probe's
    arguments rather than the ledger's ownership rule.
    """
    ledger, clock = _ledger()
    unit_a = _acquire(
        ledger,
        work_unit_id="WU-D2-12-A",
        blinded_alias="MODEL-A",
        branch="feat/wu-d2-12-a",
        worktree="/wt/wu-d2-12-a",
    )
    unit_b = _acquire(
        ledger,
        work_unit_id="WU-D2-12-B",
        blinded_alias="MODEL-B",
        branch="feat/wu-d2-12-b",
        worktree="/wt/wu-d2-12-b",
    )
    observed_at = clock.now()

    both_live = unit_a.is_live_at(observed_at) and unit_b.is_live_at(observed_at)
    live_ids = sorted(lease.lease_id for lease in ledger.active_leases())
    worktree_owners = {
        unit_a.worktree: getattr(ledger.owner_of_worktree(unit_a.worktree), "lease_id", None),
        unit_b.worktree: getattr(ledger.owner_of_worktree(unit_b.worktree), "lease_id", None),
    }
    branch_owners = {
        unit_a.branch: getattr(ledger.owner_of_branch(unit_a.branch), "lease_id", None),
        unit_b.branch: getattr(ledger.owner_of_branch(unit_b.branch), "lease_id", None),
    }

    # Negative controls: the exclusivity that makes the distinctness mean anything.
    refusals: dict[str, dict[str, Any]] = {}
    conflicts: list[tuple[str, dict[str, Any], tuple[type[Exception], ...]]] = [
        (
            "third_work_unit_claims_worker_a_worktree",
            {
                "work_unit_id": "WU-D2-12-C",
                "worktree": unit_a.worktree,
                "branch": "feat/wu-d2-12-c",
            },
            (OwnershipConflict,),
        ),
        (
            "fourth_work_unit_claims_worker_b_branch",
            {
                "work_unit_id": "WU-D2-12-D",
                "worktree": "/wt/wu-d2-12-d",
                "branch": unit_b.branch,
            },
            (OwnershipConflict,),
        ),
        (
            "second_live_lease_over_work_unit_a",
            {
                "work_unit_id": unit_a.work_unit_id,
                "worktree": unit_a.worktree,
                "branch": unit_a.branch,
            },
            (WorkUnitAlreadyLeased, OwnershipConflict),
        ),
    ]
    for label, kwargs, expected_exceptions in conflicts:
        # Any refusal keeps the second lease from existing, but *which* refusal
        # matters: a worktree probe turned away for some unrelated reason would
        # record a green for a rule it never reached.
        try:
            minted = _acquire(ledger, **kwargs)
        except LeaseError as exc:
            refusals[label] = {
                "refused": True,
                "raised": type(exc).__name__,
                "expected_refusal": [e.__name__ for e in expected_exceptions],
                "refused_for_the_expected_reason": isinstance(exc, expected_exceptions),
                "detail": str(exc),
            }
        else:
            refusals[label] = {"refused": False, "minted_lease_id": minted.lease_id}

    findings: list[str] = []
    if unit_a.lease_id == unit_b.lease_id:
        findings.append("the two work units were issued the same lease id")
    if unit_a.worktree == unit_b.worktree:
        findings.append("the two work units were issued the same worktree")
    if unit_a.branch == unit_b.branch:
        findings.append("the two work units were issued the same branch")
    if not both_live:
        findings.append(
            "the two leases are not live at one observed instant, so they are not parallel "
            "work units at all"
        )
    if live_ids != sorted([unit_a.lease_id, unit_b.lease_id]):
        findings.append(f"the ledger reports live leases {live_ids}, not exactly the two acquired")
    if worktree_owners[unit_a.worktree] != unit_a.lease_id:
        findings.append(
            f"worktree {unit_a.worktree!r} resolves to owner "
            f"{worktree_owners[unit_a.worktree]!r}, not to lease {unit_a.lease_id}"
        )
    if worktree_owners[unit_b.worktree] != unit_b.lease_id:
        findings.append(
            f"worktree {unit_b.worktree!r} resolves to owner "
            f"{worktree_owners[unit_b.worktree]!r}, not to lease {unit_b.lease_id}"
        )
    if branch_owners[unit_a.branch] != unit_a.lease_id:
        findings.append(
            f"branch {unit_a.branch!r} resolves to owner "
            f"{branch_owners[unit_a.branch]!r}, not to lease {unit_a.lease_id}"
        )
    if branch_owners[unit_b.branch] != unit_b.lease_id:
        findings.append(
            f"branch {unit_b.branch!r} resolves to owner "
            f"{branch_owners[unit_b.branch]!r}, not to lease {unit_b.lease_id}"
        )
    findings.extend(
        f"{label}: the ledger minted a second live lease over an owned identifier "
        f"({record.get('minted_lease_id')})"
        for label, record in refusals.items()
        if not record["refused"]
    )
    findings.extend(
        f"{label}: refused with {record['raised']}, not {record['expected_refusal']}; the "
        "probe did not reach the ownership rule it claims to test"
        for label, record in refusals.items()
        if record["refused"] and not record["refused_for_the_expected_reason"]
    )

    execution_log = {
        "check": a.method or "concurrent_assignment_probe",
        "expected": a.expected,
        "observed_at": observed_at.isoformat(),
        "work_units": [_lease_record(unit_a), _lease_record(unit_b)],
        "distinct_lease_ids": unit_a.lease_id != unit_b.lease_id,
        "distinct_worktrees": unit_a.worktree != unit_b.worktree,
        "distinct_branches": unit_a.branch != unit_b.branch,
        "both_live_at_the_observed_instant": both_live,
        "live_lease_ids": live_ids,
        "worktree_owner_by_identifier": worktree_owners,
        "branch_owner_by_identifier": branch_owners,
        "event_counts": _event_counts(ledger),
        "what_a_worktree_is_here": (
            "a Section 9.5 ownership identifier recorded on the lease. The ledger enforces "
            "at most one live lease per worktree/branch string; this probe creates no git "
            "worktree on disk and proves nothing about filesystem isolation."
        ),
        "what_parallel_means_here": (
            "overlapping lease liveness at one observed instant, not OS threads. "
            "InMemoryLeaseLedger serialises acquisition through its caller rather than a "
            "lock, so this is exclusivity by record, not by mutual exclusion."
        ),
    }
    negative_control = {
        "probe": (
            "with both work units live, acquire a third lease over an already-owned "
            "worktree, over an already-owned branch, and over an already-leased work unit"
        ),
        "why": (
            "two uuid lease ids are distinct however the ledger behaves. Unless a second "
            "claim on an owned identifier is refused, 'distinct worktrees' is a statement "
            "about this probe's arguments and not about ownership."
        ),
        "attempts": refusals,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        "two simultaneously live leases hold distinct lease ids, worktrees and branches, "
        "and a third claim on either owned identifier is refused",
    )


# ===========================================================================
# A2 — a submission from an expired lease is rejected
# ===========================================================================

def d2_12_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``expire_lease_then_submit`` → ``rejected_with STALE_ASSIGNMENT``.

    Two arms, because expiry and reassignment are different facts and only one
    of them is what this assertion names. The first expires the lease with
    nobody else holding the work unit, so the clock is the only thing that can
    make the submission stale -- if the fence needs a successor to notice, it is
    not checking expiry at all. The second is the pack's KB-001 shape, expired
    then reassigned, which is how it happens in practice.

    The negative control is the same construction stopped one second short of
    expiry: that submission must be accepted, and recorded as accepted. Without
    it this check would pass against a fence that rejects everything.
    """
    ledger, clock = _ledger()
    lease = _acquire(ledger)
    duration = lease.renewal_policy.lease_duration_seconds
    gateway = SubmissionGateway(LedgerFencingOracle(ledger))
    clock.advance(duration + 1)
    expired_verdict = gateway.submit(_submission(lease))

    reassigned_ledger, reassigned_clock = _ledger()
    old = _acquire(reassigned_ledger)
    reassigned_clock.advance(duration + 1)
    successor = _acquire(reassigned_ledger)
    reassigned_verdict = SubmissionGateway(LedgerFencingOracle(reassigned_ledger)).submit(
        _submission(old)
    )

    live_ledger, live_clock = _ledger()
    live_lease = _acquire(live_ledger)
    live_gateway = SubmissionGateway(LedgerFencingOracle(live_ledger))
    live_clock.advance(duration - 1)
    live_verdict = live_gateway.submit(_submission(live_lease))
    live_events = _event_counts(live_ledger)

    minted_expired = _minted_oracle_decision(ctx, "KB-001")
    minted_live = _minted_oracle_decision(ctx, "KG-001")

    findings: list[str] = []
    if not _is_stale(expired_verdict):
        findings.append(
            "a submission one second past expiry produced "
            f"{expired_verdict.verdict.value}/{expired_verdict.resulting_task_state}"
        )
    if _REASON_EXPIRED not in expired_verdict.reasons:
        findings.append(f"the rejection does not cite expiry: {expired_verdict.reasons}")
    if not _is_stale(reassigned_verdict):
        findings.append(
            "a submission from a lease that expired and was reassigned produced "
            f"{reassigned_verdict.verdict.value}/{reassigned_verdict.resulting_task_state}"
        )
    if live_verdict.verdict is not Verdict.PASS or live_verdict.resulting_task_state is not None:
        findings.append(
            "negative control failed: a submission from a live, unexpired lease was not "
            f"accepted ({live_verdict.verdict.value}, reasons={live_verdict.reasons}). "
            "A fence that rejects everything fences nothing."
        )
    if live_events.get(LeaseEventType.SUBMISSION_ACCEPTED.value, 0) != 1:
        findings.append(
            f"the accepted submission was not recorded as SubmissionAccepted: {live_events}"
        )
    if minted_expired["observed_verdict"] != Verdict.FAIL.value or (
        minted_expired["failure_state"] != TaskState.STALE_ASSIGNMENT.value
    ):
        findings.append(f"minted ORACLE-002 disagrees on KB-001: {minted_expired}")
    if minted_live["observed_verdict"] != Verdict.PASS.value:
        findings.append(f"minted ORACLE-002 disagrees on KG-001: {minted_live}")

    execution_log = {
        "check": a.method or "expire_lease_then_submit",
        "expected": a.expected,
        "lease_duration_seconds": duration,
        "expired_and_unreassigned": {
            "lease": _lease_record(lease),
            **_verdict_record(
                "submit one second after expiry, work unit not reassigned", expired_verdict
            ),
            "expiry_alone_was_sufficient": expired_verdict.reasons == [_REASON_EXPIRED],
        },
        "expired_and_reassigned": {
            "expired_lease": _lease_record(old),
            "successor_lease": _lease_record(successor),
            **_verdict_record(
                "KB-001 shape: expired, work unit reassigned, then submit", reassigned_verdict
            ),
        },
        "minted_oracle_cross_check": minted_expired,
        "time_is_system_measured": (
            "the decision uses the ledger clock's observed instant; claimed_submitted_at is "
            "recorded and never consulted (Section 9.8, ORACLE-002 GP-003)"
        ),
    }
    negative_control = {
        "probe": "identical construction, submitted one second before expiry",
        "why": (
            "'rejected as stale' is satisfied by a fence that rejects every submission ever "
            "made. This arm must be accepted, so the check distinguishes rejection-of-stale "
            "from rejection-of-everything."
        ),
        **_verdict_record("live lease, one second before expiry", live_verdict),
        "ledger_events": live_events,
        "minted_oracle_known_good": minted_live,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        "an expired lease's submission is rejected as STALE_ASSIGNMENT, with and without a "
        "successor, while the same submission one second earlier is accepted",
    )


# ===========================================================================
# A3 — a submission from a superseded lease generation is rejected
# ===========================================================================

def d2_12_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``increment_generation_then_submit_old`` → ``rejected_with STALE_ASSIGNMENT``.

    Deliberately distinct from A2: this lease has *not* expired. It lost the
    work unit to a strictly higher generation and would still look alive to
    anything that only consulted a clock. That is the failure ORACLE-002 exists
    for -- both submissions are well-formed, and only the generation and the
    ledger's memory of who was superseded tell them apart.

    The GP-001 arm is included because the obvious way to defeat generation
    fencing is to heartbeat at submission time and claim currency. The ledger
    must refuse that renewal outright, and the submission must still be stale
    afterwards; a fence that merely re-read ``expires_at`` would go green here.

    The negative control is the winner's own submission through the same
    gateway, which must be accepted -- otherwise "the old generation is
    rejected" would only mean "this work unit is closed to everyone".
    """
    ledger, clock = _ledger()
    superseded = _acquire(ledger)
    clock.advance(1)
    winner = _acquire(ledger, supersede=True, blinded_alias="MODEL-B")
    gateway = SubmissionGateway(LedgerFencingOracle(ledger))

    old_verdict = gateway.submit(_submission(superseded))

    try:
        renewed = ledger.renew(superseded.lease_id)
    except LeaseError as exc:
        renewal = {"refused": True, "raised": type(exc).__name__, "detail": str(exc)}
    else:
        renewal = {"refused": False, "renewed_expires_at": renewed.expires_at.isoformat()}
    after_renewal_verdict = gateway.submit(_submission(superseded))

    winner_verdict = gateway.submit(_submission(winner))
    events = _event_counts(ledger)
    superseded_record = ledger.get(superseded.lease_id)

    minted_resurrection = _minted_oracle_decision(ctx, "GP-001")
    minted_current = _minted_oracle_decision(ctx, "KG-001")

    findings: list[str] = []
    if winner.generation <= superseded.generation:
        findings.append(
            f"generations are not strictly increasing: {superseded.generation} then "
            f"{winner.generation}"
        )
    if not _is_stale(old_verdict):
        findings.append(
            f"a submission from generation {superseded.generation} while "
            f"{winner.generation} is current produced "
            f"{old_verdict.verdict.value}/{old_verdict.resulting_task_state}"
        )
    for reason in (_REASON_GENERATION_BELOW_CURRENT, _REASON_SUPERSEDED):
        if reason not in old_verdict.reasons:
            findings.append(f"the rejection does not cite {reason}: {old_verdict.reasons}")
    if not renewal["refused"]:
        findings.append(
            f"GP-001: the ledger renewed a superseded lease, resurrecting a dead "
            f"generation ({renewal})"
        )
    if not _is_stale(after_renewal_verdict):
        findings.append(
            "GP-001: after a renewal attempt the dead generation's submission produced "
            f"{after_renewal_verdict.verdict.value}/{after_renewal_verdict.resulting_task_state}"
        )
    if winner_verdict.verdict is not Verdict.PASS or winner_verdict.resulting_task_state is not None:
        findings.append(
            "negative control failed: the current generation's own submission was not "
            f"accepted ({winner_verdict.verdict.value}, reasons={winner_verdict.reasons})"
        )
    if minted_resurrection["observed_verdict"] != Verdict.FAIL.value or (
        minted_resurrection["failure_state"] != TaskState.STALE_ASSIGNMENT.value
    ):
        findings.append(f"minted ORACLE-002 disagrees on GP-001: {minted_resurrection}")
    if minted_current["observed_verdict"] != Verdict.PASS.value:
        findings.append(f"minted ORACLE-002 disagrees on KG-001: {minted_current}")

    execution_log = {
        "check": a.method or "increment_generation_then_submit_old",
        "expected": a.expected,
        "superseded_lease": {
            **_lease_record(superseded),
            "superseded_at": (
                superseded_record.superseded_at.isoformat()
                if superseded_record is not None and superseded_record.superseded_at is not None
                else None
            ),
        },
        "current_lease": _lease_record(winner),
        "generations_strictly_increasing": winner.generation > superseded.generation,
        "superseded_lease_had_not_expired": not superseded.is_expired_at(clock.now()),
        "old_generation_submission": _verdict_record(
            "submit from generation n while n+1 holds the work unit", old_verdict
        ),
        "gp_001_renewal_at_submission_time": renewal,
        "submission_after_renewal_attempt": _verdict_record(
            "GP-001: heartbeat, then resubmit from the dead generation", after_renewal_verdict
        ),
        "minted_oracle_cross_check": minted_resurrection,
        "event_counts": events,
    }
    negative_control = {
        "probe": "the winning generation submits on the same ledger through the same fence",
        "why": (
            "rejecting the old generation is only fencing if the new one gets through. A "
            "fence that closed the work unit to everybody would satisfy the assertion's "
            "words and none of its intent."
        ),
        **_verdict_record("current generation submits", winner_verdict),
        "minted_oracle_known_good": minted_current,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        "an unexpired but superseded generation is rejected as STALE_ASSIGNMENT, renewal "
        "does not resurrect it, and the current generation is still accepted",
    )


# ===========================================================================
# A4 — a rejected stale submission does not corrupt the winning branch
# ===========================================================================

class _BranchApplier:
    """Stands exactly where a branch write would stand.

    ``SubmissionGateway`` invokes its applier only after the fence returns
    ``PASS``; a real integrator (git push, merge-queue entry, artifact commit)
    is passed in as that callable. Recording it is therefore not a mock of the
    subject under test -- gateway, oracle and ledger are all real -- it is an
    observation point on the single edge that could mutate a branch. If it is
    never invoked, no write reached the branch.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.branches: dict[str, list[str]] = {}

    def __call__(self, submission: LedgerSubmission) -> None:
        self.calls.append(
            {
                "lease_id": submission.lease_id,
                "generation": submission.lease_generation,
                "branch": submission.branch,
                "artifact_hashes": dict(submission.artifact_hashes),
            }
        )
        self.branches.setdefault(submission.branch, []).extend(
            sorted(submission.artifact_hashes.values())
        )

    def state_hash(self) -> str:
        return content_hash(self.branches)


def d2_12_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``branch_integrity_check`` → ``winning_branch_unmodified``.

    The winner takes the work unit, submits, and its artifact is applied. The
    displaced worker then does what GP-004 describes: it submits over the
    winner's branch. Three things are then asserted about that second
    submission, and the last two are the load-bearing ones -- the fence rejected
    it, the applier was never invoked, and the branch state hashes identically
    to the snapshot taken before.

    "Unmodified" is free for a callback that is never called at all, so the
    winner's accepted submission earlier in the same probe -- same gateway, same
    applier instance -- is the negative control: it shows the write path is live
    and would have carried the stale worker's artifact had the fence let it
    through.

    Honest limit: the branch is modelled by this applier, not by git. What is
    proven is that no write path is reachable from a rejected verdict -- the
    fence runs before the applier in ``SubmissionGateway.submit``, and there is
    no other route to it -- not that an on-disk git branch was left alone.
    """
    ledger, clock = _ledger()
    stale = _acquire(ledger)
    clock.advance(1)
    winner = _acquire(ledger, supersede=True, blinded_alias="MODEL-B")

    applier = _BranchApplier()
    gateway = SubmissionGateway(LedgerFencingOracle(ledger), applier=applier)

    winner_verdict = gateway.submit(
        _submission(winner, artifact_hashes={"candidate": "sha256:winner-artifact"})
    )
    calls_after_winner = len(applier.calls)
    state_before = {branch: list(values) for branch, values in applier.branches.items()}
    hash_before = applier.state_hash()

    stale_verdict = gateway.submit(
        _submission(
            stale,
            branch=winner.branch,
            worktree=winner.worktree,
            artifact_hashes={"candidate": "sha256:stale-force-push"},
        )
    )
    calls_after_stale = len(applier.calls)
    state_after = {branch: list(values) for branch, values in applier.branches.items()}
    hash_after = applier.state_hash()
    events = _event_counts(ledger)

    stale_artifact_present = any(
        "sha256:stale-force-push" in values for values in applier.branches.values()
    )

    findings: list[str] = []
    if winner_verdict.verdict is not Verdict.PASS:
        findings.append(
            "negative control failed: the winning lease's own submission was not accepted "
            f"({winner_verdict.verdict.value}, reasons={winner_verdict.reasons}), so a later "
            "'branch unmodified' would prove only that nothing is ever applied"
        )
    if calls_after_winner != 1:
        findings.append(
            f"the applier was invoked {calls_after_winner} time(s) for the accepted "
            "submission; the write path being observed is not the one that runs"
        )
    if not _is_stale(stale_verdict):
        findings.append(
            "the displaced worker's submission produced "
            f"{stale_verdict.verdict.value}/{stale_verdict.resulting_task_state}"
        )
    if calls_after_stale != calls_after_winner:
        findings.append(
            f"the applier was invoked {calls_after_stale - calls_after_winner} extra time(s) "
            "for a rejected submission; a rejected verdict reaches a write path"
        )
    if hash_after != hash_before:
        findings.append(
            f"the winning branch state changed after a rejected submission: "
            f"{hash_before} -> {hash_after}"
        )
    if stale_artifact_present:
        findings.append("the stale worker's artifact is present in the winning branch state")
    if events.get(LeaseEventType.SUBMISSION_ACCEPTED.value, 0) != 1:
        findings.append(f"expected exactly one SubmissionAccepted event, got {events}")
    if events.get(LeaseEventType.SUBMISSION_REJECTED.value, 0) < 1:
        findings.append(f"the rejection was never recorded in the task ledger: {events}")

    execution_log = {
        "check": a.method or "branch_integrity_check",
        "expected": a.expected,
        "winning_lease": _lease_record(winner),
        "displaced_lease": _lease_record(stale),
        "winner_submission": _verdict_record("winning generation submits", winner_verdict),
        "stale_submission": _verdict_record(
            "GP-004: displaced generation submits over the winner's branch", stale_verdict
        ),
        "branch_state_before": state_before,
        "branch_state_after": state_after,
        "branch_state_hash_before": hash_before,
        "branch_state_hash_after": hash_after,
        "applier_calls": applier.calls,
        "applier_invocations_for_the_rejected_submission": calls_after_stale - calls_after_winner,
        "event_counts": events,
        "how_the_branch_is_modelled": (
            "SubmissionGateway invokes its applier callback only after the fence returns "
            "PASS; this probe substitutes a recording applier for that callback. Gateway, "
            "oracle and ledger are the real ones. No git branch is written, so what is "
            "proven is that no write path is reachable from a rejected verdict -- not that "
            "an on-disk branch was inspected."
        ),
    }
    negative_control = {
        "probe": (
            "the same gateway and the same applier instance accepted and applied the "
            "winner's submission immediately before the stale one"
        ),
        "why": (
            "'winning branch unmodified' is satisfied for free by an applier that is never "
            "called. This arm shows the write path is live, so the stale submission's "
            "failure to reach it is the fence's doing and not an inert probe."
        ),
        "winner_applied": applier.calls[:calls_after_winner],
        "branch_state_after_the_accepted_submission": state_before,
        "branch_state_after_the_rejected_submission": state_after,
        "unchanged": hash_after == hash_before,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "branch_state_hash_before": hash_before,
            "branch_state_hash_after": hash_after,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        "the stale submission was rejected before any write path was reached and the winning "
        "branch state hash is unchanged, while the winner's own submission was applied",
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D2_12: dict[tuple[str, str], Check] = {
    ("GATE-D2-12", "A1"): d2_12_a1,
    ("GATE-D2-12", "A2"): d2_12_a2,
    ("GATE-D2-12", "A3"): d2_12_a3,
    ("GATE-D2-12", "A4"): d2_12_a4,
}
