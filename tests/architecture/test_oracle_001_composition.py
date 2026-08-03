"""Pinned checker test suite for ORACLE-001 (composition reachability).

The path is pinned by the oracle definition itself
(``pinned_checker_test_suite: tests/architecture/test_oracle_001_composition.py``),
which is why this file lives under ``tests/architecture`` rather than beside
the other oracle suites. Contract Section 17.4 requires a *pinned* checker
suite; a suite at a different path is not the pinned one.

These tests fail when the oracle is wrong, not when it is merely different.
Each one names the definition clause it protects.
"""

from __future__ import annotations

import pytest

from governance.states import TaskState, Verdict
from oracles import fixtures as fx
from oracles.definitions import load_definition
from oracles.no_judge import prove_no_judge
from oracles.oracle_001_composition import CompositionReachabilityOracle


@pytest.fixture(scope="module")
def oracle() -> CompositionReachabilityOracle:
    return CompositionReachabilityOracle(load_definition("ORACLE-001"))


def test_known_good_composition_passes(oracle):
    decision = oracle.decide(fx.good_composition())
    assert decision.verdict is Verdict.PASS, decision.reasons


@pytest.mark.parametrize(
    "fixture_id",
    [f.fixture_id for f in fx.fixtures_for("ORACLE-001") if f.kind == fx.KNOWN_BAD],
)
def test_every_known_bad_fixture_fails(oracle, fixture_id):
    fixture = next(f for f in fx.fixtures_for("ORACLE-001") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, f"{fixture_id}: {fixture.description}"
    assert decision.failure_state == fixture.expected_failure_state


@pytest.mark.parametrize(
    "fixture_id",
    [f.fixture_id for f in fx.fixtures_for("ORACLE-001") if f.kind == fx.GAMING_PROBE],
)
def test_every_gaming_probe_still_fails(oracle, fixture_id):
    """A probe that stops failing is a hole in the oracle, not a passing build."""
    fixture = next(f for f in fx.fixtures_for("ORACLE-001") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, f"{fixture_id}: {fixture.description}"


def test_registration_alone_is_not_reachability(oracle):
    """GP-001. The single most tempting way to make this oracle green."""
    snapshot = fx.good_composition()
    snapshot.invocation_edges = [("api", "projects"), ("projects", "tasks")]
    snapshot.import_edges = list(snapshot.invocation_edges)
    assert "evaluation" in snapshot.registered_modules
    decision = oracle.decide(snapshot)
    assert decision.verdict is Verdict.FAIL
    assert decision.failure_state == TaskState.FAILED_WIRING
    assert any("unreachable" in reason for reason in decision.reasons)


def test_placeholder_wiring_field_is_not_a_declaration(oracle):
    """GP-003. Section 5.2 requires the nine fields to be *proven*, not present."""
    for field_name, value in (
        ("health_check", "TODO"),
        ("e2e_path", ""),
        ("telemetry_span", "placeholder"),
        ("dashboard_projection", "TBD"),
    ):
        snapshot = fx.good_composition()
        setattr(snapshot.wiring["evaluation"], field_name, value)
        decision = oracle.decide(snapshot)
        assert decision.verdict is Verdict.FAIL, field_name
        assert decision.failure_state == TaskState.FAILED_WIRING


def test_unverifiable_is_not_a_soft_pass(oracle):
    for fixture in fx.fixtures_for("ORACLE-001"):
        if fixture.kind != fx.UNVERIFIABLE_PROBE:
            continue
        decision = oracle.decide(fixture.subject)
        assert decision.verdict is Verdict.UNVERIFIABLE, fixture.fixture_id
        assert decision.verdict is not Verdict.PASS


def test_health_carries_every_declared_field(oracle):
    result = oracle.evaluate(fx.good_composition(), subject_ref="KG-001")
    declared = oracle.declared_health_fields
    assert declared, "the definition declares no health fields"
    assert sorted(result.health) == sorted(declared)


def test_no_model_call_in_the_verdict_path():
    """Section 17.4 structural proof, checked here and not merely asserted."""
    proof = prove_no_judge("oracles.oracle_001_composition")
    assert proof.holds, proof.violations
    assert proof.modules_in_closure
