"""Pinned checker test suite for ORACLE-003 (provenance binding).

Path pinned by the oracle definition. Contract Sections 8, 18, 23.

This oracle enforces the contract's shortest and most load-bearing sentence:
*"Done" without named evidence is invalid.* The tests below check the two
things that make it enforceable rather than decorative -- that the content hash
is recomputed rather than trusted, and that a missing hash is a determinate
FAIL rather than an UNVERIFIABLE.
"""

from __future__ import annotations

import pytest

from governance.envelope import CompiledObject, EvidenceTier
from governance.states import DriftFinding, TaskState, Verdict
from oracles import fixtures as fx
from oracles.definitions import load_definition
from oracles.no_judge import prove_no_judge
from oracles.oracle_003_provenance import (
    ClaimedResult,
    ExecutedTestRecord,
    ProvenanceBindingOracle,
    ProvenanceSubject,
)


@pytest.fixture(scope="module")
def oracle() -> ProvenanceBindingOracle:
    return ProvenanceBindingOracle(load_definition("ORACLE-003"))


def test_fully_bound_result_passes(oracle):
    decision = oracle.decide(fx.good_provenance())
    assert decision.verdict is Verdict.PASS, decision.reasons


@pytest.mark.parametrize(
    "fixture_id",
    [f.fixture_id for f in fx.fixtures_for("ORACLE-003") if f.kind == fx.KNOWN_BAD],
)
def test_every_known_bad_fixture_fails(oracle, fixture_id):
    fixture = next(f for f in fx.fixtures_for("ORACLE-003") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, fixture.description
    assert decision.failure_state == fixture.expected_failure_state


@pytest.mark.parametrize(
    "fixture_id",
    [f.fixture_id for f in fx.fixtures_for("ORACLE-003") if f.kind == fx.GAMING_PROBE],
)
def test_every_gaming_probe_still_fails(oracle, fixture_id):
    fixture = next(f for f in fx.fixtures_for("ORACLE-003") if f.fixture_id == fixture_id)
    decision = oracle.decide(fixture.subject)
    assert decision.verdict is Verdict.FAIL, fixture.description


def test_hash_is_recomputed_not_trusted(oracle):
    """GP-002. A hash copied from a valid artifact must not validate this one."""
    obj = CompiledObject.create(
        schema_id="efah.gate_result", created_by_alias="oracle-o02", body={"verdict": "PASS"}
    )
    result = ClaimedResult.from_compiled_object(
        obj,
        result_id="R-tampered",
        evidence_artifacts=[fx.known_good_evidence_ref()],
        test_record=ExecutedTestRecord(
            command="pytest -q",
            environment="linux",
            timestamp="2026-08-02T00:00:00Z",
            exit_status=0,
            raw_result_artifact="evidence/x.json",
            commit_binding="0f00a7a",
        ),
    )
    subject = ProvenanceSubject(results=[result], current_contract_version="1.1")
    assert oracle.decide(subject).verdict is Verdict.PASS

    result.body = {"verdict": "FAIL"}  # same envelope, different body
    decision = oracle.decide(subject)
    assert decision.verdict is Verdict.FAIL
    assert decision.failure_state == TaskState.FAILED_PROVENANCE
    assert any("does not match recomputed" in reason for reason in decision.reasons)


def test_missing_evidence_is_a_fail_not_an_unverifiable(oracle):
    """The definition is explicit: absence of provenance is a determinate answer."""
    subject = fx.good_provenance(results=[fx.good_claimed_result(evidence_artifacts=[])])
    decision = oracle.decide(subject)
    assert decision.verdict is Verdict.FAIL
    assert decision.verdict is not Verdict.UNVERIFIABLE


def test_stale_contract_version_is_named_as_such(oracle):
    subject = fx.good_provenance(results=[fx.good_claimed_result(contract_version="0.2")])
    decision = oracle.decide(subject)
    assert decision.failure_state == DriftFinding.STALE_CONTRACT_VERSION


def test_deterministic_tier_cannot_be_claimed_for_a_judged_result(oracle):
    """GP-003. The tier is cross-checked against the verdict path."""
    subject = fx.good_provenance(
        results=[
            fx.good_claimed_result(
                evidence_tier=EvidenceTier.DETERMINISTIC_ORACLE.value, verdict_path="model_judge"
            )
        ]
    )
    decision = oracle.decide(subject)
    assert decision.verdict is Verdict.FAIL
    assert any("DETERMINISTIC_ORACLE" in reason for reason in decision.reasons)


def test_service_outage_is_the_only_unverifiable(oracle):
    assert oracle.decide(fx.good_provenance(terminus_service_available=False)).verdict is (
        Verdict.UNVERIFIABLE
    )
    assert oracle.decide(fx.good_provenance(evidence_storage_available=False)).verdict is (
        Verdict.UNVERIFIABLE
    )


def test_health_reports_unresolvable_references(oracle):
    result = oracle.evaluate(
        fx.good_provenance(results=[fx.good_claimed_result(terminus_commit_resolvable=False)])
    )
    assert sorted(result.health) == sorted(oracle.declared_health_fields)
    assert result.health["unresolvable_reference_count"] >= 1


def test_no_model_call_in_the_verdict_path():
    proof = prove_no_judge("oracles.oracle_003_provenance")
    assert proof.holds, proof.violations
