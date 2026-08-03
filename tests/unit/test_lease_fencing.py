"""ORACLE-002 and GATE-D2-12 -- leases, worktrees, and stale-worker rejection.

Contract Section 9.5. The negative controls are the point of this file: a
fencing implementation that never rejects anything passes every happy-path test
ever written.
"""

from __future__ import annotations

import pytest

from assignments.fencing import (
    ORACLE_VERSION,
    LeaseFencingOracle,
    Submission,
    SubmissionGateway,
    fixture_report,
    run_fixture_suite,
)
from assignments.leases import (
    LEASE_DURATION_SECONDS,
    LEASE_RENEWAL_HEARTBEAT_SECONDS,
    AssignmentLease,
    InMemoryLeaseLedger,
    LeaseExpiredError,
    ManualClock,
    OwnershipConflict,
    OwnershipMode,
    RenewalPolicy,
    WorkUnitAlreadyLeased,
)
from governance.states import TaskState, Verdict


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def ledger(clock: ManualClock) -> InMemoryLeaseLedger:
    return InMemoryLeaseLedger(clock=clock)


def acquire(ledger: InMemoryLeaseLedger, **overrides) -> AssignmentLease:
    params = {
        "work_unit_id": "WU-0042",
        "role": "implementer",
        "blinded_alias": "MODEL-A",
        "branch": "feat/wu-0042",
        "worktree": "/wt/wu-0042",
        "input_hashes": {"work_unit": "sha256:aaa"},
        "permitted_output_schemas": ("efah.work_unit_candidate",),
    }
    params.update(overrides)
    return ledger.acquire(**params)


def submission_for(lease: AssignmentLease, **overrides) -> Submission:
    params = {
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
    return Submission(**params)


# --- Section 9.5 required lease fields --------------------------------------


def test_a_lease_carries_every_section_9_5_field(ledger: InMemoryLeaseLedger):
    lease = acquire(ledger)
    assert lease.role and lease.blinded_alias
    assert lease.ownership_mode is OwnershipMode.EXCLUSIVE
    assert lease.lease_id and lease.generation == 1
    assert lease.expires_at > lease.acquired_at
    assert lease.renewal_policy.heartbeat_seconds == LEASE_RENEWAL_HEARTBEAT_SECONDS
    assert lease.branch and lease.worktree
    assert lease.input_hashes == {"work_unit": "sha256:aaa"}
    assert lease.permitted_output_schemas == ("efah.work_unit_candidate",)


def test_durations_come_from_the_autonomy_policy():
    policy = RenewalPolicy()
    assert policy.lease_duration_seconds == LEASE_DURATION_SECONDS == 1800
    assert policy.heartbeat_seconds == LEASE_RENEWAL_HEARTBEAT_SECONDS == 300
    assert policy.heartbeats_per_lease == 6


# --- GATE-D2-12 A1: distinct leases, distinct worktrees ---------------------


def test_gate_d2_12_a1_parallel_work_units_hold_distinct_leases_and_worktrees(
    ledger: InMemoryLeaseLedger,
):
    first = acquire(ledger)
    second = acquire(ledger, work_unit_id="WU-0043", branch="feat/wu-0043", worktree="/wt/wu-0043")
    assert first.lease_id != second.lease_id
    assert first.worktree != second.worktree
    assert {lease.lease_id for lease in ledger.active_leases()} == {first.lease_id, second.lease_id}


def test_a_second_worker_cannot_take_a_live_exclusive_work_unit(ledger: InMemoryLeaseLedger):
    acquire(ledger)
    with pytest.raises(WorkUnitAlreadyLeased):
        acquire(ledger)


def test_two_work_units_cannot_share_a_worktree(ledger: InMemoryLeaseLedger):
    acquire(ledger)
    with pytest.raises(OwnershipConflict):
        acquire(ledger, work_unit_id="WU-0043", branch="feat/wu-0043")


def test_two_work_units_cannot_share_a_branch(ledger: InMemoryLeaseLedger):
    acquire(ledger)
    with pytest.raises(OwnershipConflict):
        acquire(ledger, work_unit_id="WU-0043", worktree="/wt/wu-0043")


# --- GATE-D2-12 A2 / A3: the negative controls ------------------------------


def test_gate_d2_12_a2_a_submission_from_an_expired_lease_is_rejected_as_stale(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    """NEGATIVE CONTROL. The whole fencing mechanism exists for this case."""
    lease = acquire(ledger)
    good = LeaseFencingOracle(ledger).evaluate(submission_for(lease))
    assert good.verdict is Verdict.PASS  # same submission, before expiry

    clock.advance(LEASE_DURATION_SECONDS + 1)
    verdict = LeaseFencingOracle(ledger).evaluate(submission_for(lease))

    assert verdict.verdict is Verdict.FAIL
    assert verdict.resulting_task_state is TaskState.STALE_ASSIGNMENT
    assert "lease_expired_before_submission_timestamp" in verdict.reasons


def test_gate_d2_12_a3_a_superseded_generation_is_rejected_as_stale(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    stale = acquire(ledger)
    clock.advance(60)
    current = acquire(ledger, supersede=True, blinded_alias="MODEL-B")
    assert current.generation == stale.generation + 1

    oracle = LeaseFencingOracle(ledger)
    verdict = oracle.evaluate(submission_for(stale))
    assert verdict.verdict is Verdict.FAIL
    assert verdict.resulting_task_state is TaskState.STALE_ASSIGNMENT
    assert "submission_generation_below_current_generation" in verdict.reasons
    assert verdict.lease_generation_submitted == 1
    assert verdict.lease_generation_current == 2

    # ... and the winner still passes.
    assert oracle.evaluate(submission_for(current)).verdict is Verdict.PASS


def test_exactly_one_of_two_concurrent_submissions_wins(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    """ORACLE-002 KB-004."""
    worker_a = acquire(ledger)
    clock.advance(5)
    worker_b = acquire(ledger, supersede=True, blinded_alias="MODEL-B")
    oracle = LeaseFencingOracle(ledger)
    verdicts = [
        oracle.evaluate(submission_for(worker_a)).verdict,
        oracle.evaluate(submission_for(worker_b)).verdict,
    ]
    assert verdicts.count(Verdict.PASS) == 1
    assert verdicts.count(Verdict.FAIL) == 1


def test_input_hash_drift_is_stale_even_at_the_current_generation(ledger: InMemoryLeaseLedger):
    lease = acquire(ledger)
    verdict = LeaseFencingOracle(ledger).evaluate(
        submission_for(lease, input_hashes={"work_unit": "sha256:tampered"})
    )
    assert verdict.verdict is Verdict.FAIL
    assert "input_hashes_differ_from_lease_record" in verdict.reasons


def test_an_output_schema_outside_the_lease_is_refused(ledger: InMemoryLeaseLedger):
    """Section 9.5 "permitted output schemas"."""
    lease = acquire(ledger)
    verdict = LeaseFencingOracle(ledger).evaluate(
        submission_for(lease, output_schema="efah.something_i_invented")
    )
    assert verdict.verdict is Verdict.FAIL
    assert "output_schema_not_permitted_by_lease" in verdict.reasons


# --- gaming probes ----------------------------------------------------------


def test_gp_001_renewal_after_expiry_does_not_resurrect_a_generation(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    lease = acquire(ledger)
    clock.advance(LEASE_DURATION_SECONDS + 1)
    with pytest.raises(LeaseExpiredError):
        ledger.renew(lease.lease_id)
    verdict = LeaseFencingOracle(ledger).evaluate(submission_for(lease))
    assert verdict.verdict is Verdict.FAIL


def test_a_live_lease_can_be_heartbeated(ledger: InMemoryLeaseLedger, clock: ManualClock):
    """The refusal above must not be achieved by refusing every renewal."""
    lease = acquire(ledger)
    clock.advance(LEASE_RENEWAL_HEARTBEAT_SECONDS)
    renewed = ledger.heartbeat(lease.lease_id)
    assert renewed.expires_at > lease.expires_at
    clock.advance(LEASE_DURATION_SECONDS - 1)
    assert LeaseFencingOracle(ledger).evaluate(submission_for(renewed)).verdict is Verdict.PASS


def test_gp_003_a_backdated_submission_timestamp_is_ignored(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    """Section 9.8: time comes from system events, not from the submitter."""
    lease = acquire(ledger)
    clock.advance(LEASE_DURATION_SECONDS + 600)
    verdict = LeaseFencingOracle(ledger).evaluate(
        submission_for(lease, claimed_submitted_at=lease.acquired_at)
    )
    assert verdict.verdict is Verdict.FAIL
    assert "lease_expired_before_submission_timestamp" in verdict.reasons
    assert verdict.health["clock_skew_observed"] > 0


def test_gp_004_a_stale_worker_cannot_claim_the_winners_branch(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    stale = acquire(ledger)
    clock.advance(5)
    winner = acquire(ledger, supersede=True, blinded_alias="MODEL-B", branch="feat/wu-0042-v2")
    verdict = LeaseFencingOracle(ledger).evaluate(submission_for(stale, branch=winner.branch))
    assert verdict.verdict is Verdict.FAIL
    assert "branch_not_owned_by_submitting_lease" in verdict.reasons


# --- UNVERIFIABLE, kept distinct from FAIL ----------------------------------


def test_a_submission_with_no_lease_identifier_is_unverifiable(ledger: InMemoryLeaseLedger):
    verdict = LeaseFencingOracle(ledger).evaluate(Submission(work_unit_id="WU-0042"))
    assert verdict.verdict is Verdict.UNVERIFIABLE
    assert verdict.resulting_task_state is None


def test_an_absent_lease_record_is_unverifiable_not_stale(ledger: InMemoryLeaseLedger):
    verdict = LeaseFencingOracle(ledger).evaluate(
        Submission(work_unit_id="WU-9999", lease_id="LEASE-nope", lease_generation=1)
    )
    assert verdict.verdict is Verdict.UNVERIFIABLE
    assert "lease_record_absent" in verdict.reasons


def test_clock_skew_on_the_expiry_boundary_is_unverifiable(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    lease = acquire(ledger)
    clock.advance(LEASE_DURATION_SECONDS)  # observed_at == expires_at exactly
    verdict = LeaseFencingOracle(ledger, clock_skew_tolerance_seconds=5.0).evaluate(
        submission_for(lease, claimed_submitted_at=lease.acquired_at)
    )
    assert verdict.verdict is Verdict.UNVERIFIABLE
    assert "clock_skew_exceeds_tolerance_and_expiry_is_ambiguous" in verdict.reasons


# --- GATE-D2-12 A4: a rejected submission changes nothing --------------------


def test_gate_d2_12_a4_a_rejected_stale_submission_never_reaches_the_branch(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    branch_contents: dict[str, str] = {"feat/wu-0042": "winner-commit"}

    def apply(sub: Submission) -> None:
        branch_contents[sub.branch] = f"applied-by-{sub.lease_id}"

    stale = acquire(ledger)
    clock.advance(5)
    acquire(ledger, supersede=True, blinded_alias="MODEL-B")

    gateway = SubmissionGateway(LeaseFencingOracle(ledger), applier=apply)
    verdict = gateway.submit(submission_for(stale))

    assert verdict.verdict is Verdict.FAIL
    assert branch_contents == {"feat/wu-0042": "winner-commit"}, "stale write reached the branch"

    events = [str(e.event) for e in ledger.events()]
    assert "SubmissionRejected" in events
    assert "SubmissionAccepted" not in events


def test_a_valid_submission_is_applied(ledger: InMemoryLeaseLedger):
    """Counter-control: the gateway is not simply rejecting everything."""
    applied: list[str] = []
    lease = acquire(ledger)
    gateway = SubmissionGateway(LeaseFencingOracle(ledger), applier=lambda s: applied.append(s.lease_id))
    assert gateway.submit(submission_for(lease)).verdict is Verdict.PASS
    assert applied == [lease.lease_id]
    assert "SubmissionAccepted" in [str(e.event) for e in ledger.events()]


# --- oracle health ----------------------------------------------------------


def test_every_verdict_carries_the_required_health_block(ledger: InMemoryLeaseLedger):
    lease = acquire(ledger)
    health = LeaseFencingOracle(ledger).evaluate(submission_for(lease)).health
    assert set(health) == {
        "oracle_version",
        "content_hash",
        "fixture_suite_result",
        "last_audit_date",
        "clock_skew_observed",
    }
    assert health["oracle_version"] == ORACLE_VERSION
    assert health["content_hash"].startswith("sha256:")


def test_the_oracle_definitions_own_fixtures_all_pass():
    report = fixture_report()
    assert set(report) == {"KG-001", "KB-001", "KB-002", "KB-003", "KB-004", "GP-001", "GP-002", "GP-003", "GP-004"}
    failed = {k: v for k, v in report.items() if v["result"] != "PASS"}
    assert failed == {}
    assert run_fixture_suite(force=True) == "PASS:9/9"


def test_the_event_log_records_lease_lifecycle_from_system_time(
    ledger: InMemoryLeaseLedger, clock: ManualClock
):
    """Section 9.2 / Section 9.8."""
    lease = acquire(ledger)
    clock.advance(LEASE_RENEWAL_HEARTBEAT_SECONDS)
    ledger.renew(lease.lease_id)
    clock.advance(10)
    acquire(ledger, supersede=True)
    kinds = [str(e.event) for e in ledger.events()]
    assert kinds == ["LeaseAcquired", "LeaseRenewed", "LeaseSuperseded", "LeaseAcquired"]
    assert [e.at for e in ledger.events()] == sorted(e.at for e in ledger.events())
