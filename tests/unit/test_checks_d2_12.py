"""GATE-D2-12's checks, and the proof that they can fail.

Contract Section 9.5 · Section 18. A gate check that passes on the real system
tells you very little on its own: the same green would appear if the check
compared a constant to itself. So every check here is exercised twice -- once
against the real ledger and fence, and once against a deliberately broken one
where the property the assertion names is false. The second run is the one that
gives the first its meaning.

Each broken subject is broken in exactly one way, and the way is named:

* a ledger that does not enforce exclusive worktree/branch ownership (A1),
* a fence that finds no submission stale (A2),
* a fence that checks expiry and nothing else (A3) -- which must still satisfy
  A2, or A3 would be measuring expiry a second time instead of generations,
* a gateway that applies before it fences (A4).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from assignments.fencing import LeaseFencingOracle, Submission, SubmissionGateway
from assignments.leases import AssignmentLease, InMemoryLeaseLedger
from evaluation import checks_d2_12
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d2_12 import CHECKS_D2_12
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict

GATE_ID = "GATE-D2-12"


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context: the oracles it exposes are the minted ORACLE-002 et al.

    The candidate commit is a stand-in because these tests are about the checks,
    not about the binding; the gate-runner test at the end uses the real HEAD.
    """
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D2_12[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


# --- broken subjects -------------------------------------------------------


class LedgerWithoutOwnershipExclusivity(InMemoryLeaseLedger):
    """A ledger that forgets which leases are live.

    ``acquire`` scans the live leases to refuse a worktree or branch somebody
    else owns. A ledger that reports none hands out the same worktree twice --
    which is precisely the state A1 exists to detect.
    """

    def _live_leases(self, moment: datetime) -> Iterator[AssignmentLease]:
        return iter(())


class FenceThatNeverRejects(LeaseFencingOracle):
    """Finds no submission stale. The shape of a fence somebody disabled."""

    def _stale_reasons(self, submission, claimed, current, observed_at) -> list[str]:  # type: ignore[override]
        return []


class FenceThatOnlyChecksExpiry(LeaseFencingOracle):
    """Consults the clock and nothing else.

    This is the plausible half-implementation: expiry is easy to see and easy to
    test, generations are the part that gets skipped. A2 must still pass against
    it and A3 must not.
    """

    def _stale_reasons(self, submission, claimed, current, observed_at) -> list[str]:  # type: ignore[override]
        reasons = super()._stale_reasons(submission, claimed, current, observed_at)
        return [reason for reason in reasons if "expired" in reason]


class GatewayThatAppliesBeforeFencing(SubmissionGateway):
    """Writes first and asks afterwards -- the ordering bug A4 is about."""

    def submit(self, submission: Submission) -> Any:
        if self.applier is not None:
            self.applier(submission)
        return super().submit(submission)


# --- the registry ----------------------------------------------------------


def test_the_registry_covers_every_assertion_the_pack_declares(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D2_12 if gid == GATE_ID}
    assert registered == declared
    assert all(gid == GATE_ID for gid, _ in CHECKS_D2_12)


def test_the_merged_registry_uses_these_checks_and_not_a_shadow(gate: GateSpec):
    """Merging this map must add checks, never silently replace existing ones.

    Written before ``checks.py`` merged the map, this asserted the keys were
    absent from ``CHECKS`` entirely. That premise expired the moment they were
    registered, and "the keys are absent" would now fail for the good reason.
    The property worth keeping is the one the original name aimed at: after the
    merge every key must resolve to *this module's* function, so a later
    duplicate registration elsewhere -- the real hazard, since ``dict.update``
    silently wins -- shows up here rather than as a gate quietly executing
    someone else's check.
    """
    for key, check in CHECKS_D2_12.items():
        assert key in CHECKS, f"{key} was never merged into the registry"
        assert CHECKS[key] is check, f"{key} resolves to a different check than this module's"


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_ledger(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["distinct_lease_ids"] and log["distinct_worktrees"]
    assert log["both_live_at_the_observed_instant"]
    assert all(
        record["refused"]
        for record in outcome.evidence["negative_control_transcript"]["attempts"].values()
    )


def test_a1_fails_when_a_worktree_can_be_owned_twice(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_12, "InMemoryLeaseLedger", LedgerWithoutOwnershipExclusivity)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("second live lease over an owned identifier" in f for f in outcome.findings)


def test_a1_records_that_a_worktree_here_is_an_identifier_not_a_directory(ctx, gate):
    """The caveat is part of the evidence, not a comment somebody can drop."""
    log = run(ctx, gate, "A1").evidence["gate_execution_log"]
    assert "no git worktree" in log["what_a_worktree_is_here"]
    assert "not OS threads" in log["what_parallel_means_here"]


# --- A2 --------------------------------------------------------------------


def test_a2_passes_against_the_real_fence(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["expired_and_unreassigned"]["resulting_task_state"] == "STALE_ASSIGNMENT"
    assert log["expired_and_unreassigned"]["expiry_alone_was_sufficient"]
    assert log["expired_and_reassigned"]["resulting_task_state"] == "STALE_ASSIGNMENT"


def test_a2_fails_when_the_fence_rejects_nothing(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_12, "LedgerFencingOracle", FenceThatNeverRejects)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("past expiry produced PASS" in f for f in outcome.findings)


def test_a2_negative_control_is_an_acceptance_not_another_rejection(ctx, gate):
    control = run(ctx, gate, "A2").evidence["negative_control_transcript"]
    assert control["verdict"] == Verdict.PASS.value
    assert control["resulting_task_state"] is None
    assert control["ledger_events"]["SubmissionAccepted"] == 1


def test_a2_would_fail_if_the_fence_rejected_the_live_lease_too(ctx, gate, monkeypatch):
    """The negative control is load-bearing: prove it can fail on its own.

    A fence that calls everything stale satisfies "an expired submission is
    rejected" perfectly. This is the arm that refuses it.
    """

    class FenceThatRejectsEverything(LeaseFencingOracle):
        def _stale_reasons(self, submission, claimed, current, observed_at):
            return ["rejecting_everything"]

    monkeypatch.setattr(checks_d2_12, "LedgerFencingOracle", FenceThatRejectsEverything)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control failed" in f for f in outcome.findings)


# --- A3 --------------------------------------------------------------------


def test_a3_passes_against_the_real_fence(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["generations_strictly_increasing"]
    # The point of A3: the loser was still inside its lease window.
    assert log["superseded_lease_had_not_expired"]
    assert log["old_generation_submission"]["resulting_task_state"] == "STALE_ASSIGNMENT"
    assert log["gp_001_renewal_at_submission_time"]["raised"] == "LeaseSupersededError"
    assert log["submission_after_renewal_attempt"]["resulting_task_state"] == "STALE_ASSIGNMENT"


def test_a3_fails_when_the_fence_only_checks_expiry(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_12, "LedgerFencingOracle", FenceThatOnlyChecksExpiry)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("is current produced PASS" in f for f in outcome.findings)


def test_a2_survives_the_expiry_only_fence_that_a3_catches(ctx, gate, monkeypatch):
    """A3 measures generation fencing, not expiry a second time.

    If A2 also failed here, the two assertions would be testing one property
    twice and a superseded-but-unexpired lease would have nothing watching it.
    """
    monkeypatch.setattr(checks_d2_12, "LedgerFencingOracle", FenceThatOnlyChecksExpiry)
    assert run(ctx, gate, "A2").status is AssertionStatus.PASS


def test_a3_negative_control_lets_the_winner_through(ctx, gate):
    control = run(ctx, gate, "A3").evidence["negative_control_transcript"]
    assert control["verdict"] == Verdict.PASS.value
    assert control["lease_generation_submitted"] == control["lease_generation_current"]


def test_a3_cross_checks_the_minted_oracle_on_the_pack_fixture(ctx, gate):
    cross = run(ctx, gate, "A3").evidence["gate_execution_log"]["minted_oracle_cross_check"]
    assert cross["fixture_id"] == "GP-001"
    assert cross["observed_verdict"] == cross["expected_verdict"] == Verdict.FAIL.value
    assert cross["failure_state"] == "STALE_ASSIGNMENT"


# --- A4 --------------------------------------------------------------------


def test_a4_passes_against_the_real_gateway(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["stale_submission"]["resulting_task_state"] == "STALE_ASSIGNMENT"
    assert log["applier_invocations_for_the_rejected_submission"] == 0
    assert log["branch_state_hash_after"] == log["branch_state_hash_before"]
    assert log["event_counts"]["SubmissionAccepted"] == 1


def test_a4_fails_when_the_gateway_applies_before_it_fences(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_12, "SubmissionGateway", GatewayThatAppliesBeforeFencing)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("reaches a write path" in f for f in outcome.findings)
    assert any("winning branch state changed" in f for f in outcome.findings)


def test_a4_fails_when_nothing_is_ever_applied(ctx, gate, monkeypatch):
    """An inert applier makes 'unmodified' vacuous, so it must not pass.

    This is the exact shape of a check that cannot fail: observe a callback that
    is never called, and report that it never wrote anything.
    """

    class GatewayThatNeverApplies(SubmissionGateway):
        def submit(self, submission: Submission) -> Any:
            self.applier, kept = None, self.applier
            try:
                return super().submit(submission)
            finally:
                self.applier = kept

    monkeypatch.setattr(checks_d2_12, "SubmissionGateway", GatewayThatNeverApplies)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("write path being observed is not the one that runs" in f for f in outcome.findings)


def test_a4_states_that_the_branch_is_modelled_by_the_applier(ctx, gate):
    log = run(ctx, gate, "A4").evidence["gate_execution_log"]
    assert "No git branch is written" in log["how_the_branch_is_modelled"]


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_emits_the_evidence_the_gate_named(ctx, gate, assertion_id):
    outcome = run(ctx, gate, assertion_id)
    assert set(gate.evidence_required) <= set(outcome.evidence)
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = run(ctx, gate, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


def test_the_registered_gate_runs_green_with_its_evidence(monkeypatch):
    """The registration entries, exercised end to end through the runner.

    This is what merging :data:`CHECKS_D2_12` into ``CHECKS`` buys: the gate
    reports EXECUTED rather than NOT_YET_EXECUTABLE, and it produces every
    artifact its own definition named.
    """
    for key, check in CHECKS_D2_12.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]
    assert result.evidence_missing == []
    assert result.executed_count == len(result.assertions) == 4
