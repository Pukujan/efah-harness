"""Pinned checker test suite for ORACLE-002 (lease generation fencing).

Path pinned by the oracle definition. Contract Sections 9.5, 18.

The property under test is narrow and unforgiving: a submission from an expired
or superseded lease generation is rejected as stale. Everything else here
exists because there are four documented ways to make a stale submission look
current, and the oracle has to survive all of them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from governance.states import TaskState, Verdict
from oracles import fixtures as fx
from oracles.definitions import load_definition
from oracles.no_judge import prove_no_judge
from oracles.oracle_002_lease_fencing import LeaseFencingOracle


@pytest.fixture(scope="module")
def oracle() -> LeaseFencingOracle:
    return LeaseFencingOracle(load_definition("ORACLE-002"))


def test_current_unexpired_lease_passes(oracle):
    decision = oracle.decide(fx.good_fencing())
    assert decision.verdict is Verdict.PASS, decision.reasons


@pytest.mark.parametrize(
    "fixture_id",
    [
        f.fixture_id
        for f in fx.fixtures_for("ORACLE-002")
        if f.kind == fx.KNOWN_BAD and f.concurrent_subjects is None
    ],
)
def test_every_known_bad_fixture_is_stale(oracle, fixture_id):
    fixture = next(f for f in fx.fixtures_for("ORACLE-002") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, fixture.description
    assert decision.failure_state == TaskState.STALE_ASSIGNMENT


@pytest.mark.parametrize(
    "fixture_id",
    [f.fixture_id for f in fx.fixtures_for("ORACLE-002") if f.kind == fx.GAMING_PROBE],
)
def test_every_gaming_probe_still_fails(oracle, fixture_id):
    fixture = next(f for f in fx.fixtures_for("ORACLE-002") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, fixture.description


def test_renewal_does_not_resurrect_a_superseded_generation(oracle):
    """GP-001, stated plainly because it is the subtle one.

    Renewing a lease extends its expiry. It does not un-supersede a generation
    that another worker has already taken over.
    """
    subject = fx.good_fencing()
    lease = subject.lease
    lease.generation = 3
    lease.expires_at = subject.observed_at + timedelta(hours=1)
    lease.superseded_generations = {3: subject.observed_at - timedelta(minutes=5)}
    subject.submission.generation = 3
    decision = oracle.decide(subject)
    assert decision.verdict is Verdict.FAIL
    assert any("resurrect" in reason for reason in decision.reasons)


def test_submitter_supplied_timestamp_is_never_authoritative(oracle):
    """GP-003. Timestamps come from system events, not from the submitter."""
    subject = fx.good_fencing()
    subject.lease.expires_at = subject.observed_at - timedelta(minutes=1)
    subject.submission.claimed_submitted_at = subject.observed_at - timedelta(minutes=10)
    decision = oracle.decide(subject)
    assert decision.verdict is Verdict.FAIL
    assert any("system event" in reason for reason in decision.reasons)


def test_concurrent_submissions_yield_exactly_one_pass(oracle):
    """KB-004. Two well-formed submissions, one work unit, one winner."""
    racers = [
        fx.good_fencing(submission=fx.good_submission(submission_id="sub-a")),
        fx.good_fencing(submission=fx.good_submission(submission_id="sub-b")),
    ]
    decisions = oracle.decide_concurrent(racers)
    passes = [d for d in decisions if d.verdict is Verdict.PASS]
    fails = [d for d in decisions if d.verdict is Verdict.FAIL]
    assert len(passes) == 1
    assert len(fails) == 1
    assert fails[0].failure_state == TaskState.STALE_ASSIGNMENT


def test_absent_lease_record_is_unverifiable_not_a_rejection(oracle):
    """A missing ledger entry is an unknown. Guessing either way would be wrong."""
    decision = oracle.decide(fx.good_fencing(lease=None))
    assert decision.verdict is Verdict.UNVERIFIABLE
    assert decision.reasons == ["lease_record_absent_from_terminusdb"]


def test_health_carries_clock_skew_as_declared(oracle):
    result = oracle.evaluate(fx.good_fencing(clock_skew_observed_seconds=1.25))
    assert sorted(result.health) == sorted(oracle.declared_health_fields)
    assert result.health["clock_skew_observed"] == 1.25


def test_no_model_call_in_the_verdict_path():
    proof = prove_no_judge("oracles.oracle_002_lease_fencing")
    assert proof.holds, proof.violations
