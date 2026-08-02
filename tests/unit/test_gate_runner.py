"""The gate runner's own rules.

Contract Sections 17, 18, 25. These tests are about the runner's honesty, not
about any single gate: that it never reports PASS for a gate it only partly
executed, never reports PASS without the evidence the gate named, and treats a
check that raises as a failure rather than skipping it.

Every one of those is a way a gate suite quietly becomes decorative.
"""

from __future__ import annotations

import pytest

from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus
from evaluation.evidence import EvidenceStore
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import EXPECTED_GATE_COUNT
from governance.states import Verdict


@pytest.fixture(scope="module")
def summary():
    runner = GateRunner()
    return runner, runner.run()


def test_all_twenty_seven_gates_are_loaded_and_run(summary):
    _, result = summary
    assert len(result.results) == EXPECTED_GATE_COUNT
    assert result.gates_refused_to_load == {}


def test_every_result_binds_to_the_same_candidate_commit(summary):
    runner, result = summary
    assert len({r.candidate_commit for r in result.results}) == 1
    assert result.results[0].candidate_commit == runner.binding.commit_sha
    assert len(runner.binding.commit_sha) == 40


def test_no_gate_result_admits_a_model_judge(summary):
    _, result = summary
    assert all(r.model_judge_in_verdict_path is False for r in result.results)


def test_a_partially_executable_gate_never_reports_pass(summary):
    _, result = summary
    partial = result.by_executability(Executability.PARTIALLY_EXECUTABLE)
    assert partial, "expected at least one partially executable gate today"
    assert all(r.verdict is not Verdict.PASS for r in partial)


def test_a_gate_with_no_checks_is_unverifiable_not_passing(summary):
    _, result = summary
    unbuilt = result.by_executability(Executability.NOT_YET_EXECUTABLE)
    assert unbuilt
    for gate in unbuilt:
        assert gate.verdict is Verdict.UNVERIFIABLE
        assert gate.executed_count == 0
        assert all(a.note for a in gate.assertions), gate.gate_id


def test_every_passing_gate_produced_every_evidence_artifact_it_named(summary):
    _, result = summary
    for gate in result.by_verdict(Verdict.PASS):
        assert gate.evidence_missing == [], f"{gate.gate_id} passed without {gate.evidence_missing}"
        produced = {artifact["name"] for artifact in gate.evidence_produced}
        assert set(gate.evidence_required) <= produced


def test_a_gate_cannot_pass_with_missing_evidence():
    """Section 18 in its bluntest form, tested rather than asserted."""
    runner = GateRunner()
    gate = runner.gates["GATE-D2-20"]
    # Run it normally: it passes and its evidence is present.
    baseline = runner.run_gate(gate)
    assert baseline.verdict is Verdict.PASS
    assert baseline.evidence_missing == []

    # Now run the same gate against an evidence store that discards artifacts.
    class ForgetfulStore(EvidenceStore):
        def add(self, gate_id, name, payload):  # type: ignore[override]
            return super().add(gate_id, "discarded", payload)

    forgetful = GateRunner(
        binding=runner.binding,
        evidence_store=ForgetfulStore(candidate_commit=runner.binding.commit_sha),
    )
    result = forgetful.run_gate(forgetful.gates["GATE-D2-20"])
    assert result.evidence_missing
    assert result.verdict is Verdict.UNVERIFIABLE


def test_a_check_that_raises_is_a_failure_not_a_skip(monkeypatch):
    runner = GateRunner()
    gate = runner.gates["GATE-D2-20"]

    def explode(ctx, g, a):
        raise RuntimeError("check exploded")

    monkeypatch.setitem(CHECKS, ("GATE-D2-20", "A1"), explode)
    result = runner.run_gate(gate)
    a1 = next(a for a in result.assertions if a.assertion_id == "A1")
    assert a1.status is AssertionStatus.FAIL
    assert "check exploded" in a1.findings[0]
    assert result.verdict is Verdict.FAIL


def test_the_summary_is_a_sealed_compiled_object(summary):
    _, result = summary
    obj = result.to_compiled_object()
    assert obj.is_intact()
    assert obj.envelope.schema_id == "efah.gate_run_summary"
    assert obj.body["candidate_commit"] == result.candidate_commit


def test_each_gate_result_is_a_sealed_compiled_object(summary):
    _, result = summary
    for gate in result.results[:5]:
        obj = gate.to_compiled_object()
        assert obj.is_intact()
        assert obj.body["gate_id"] == gate.gate_id


def test_running_a_single_gate_is_supported():
    runner = GateRunner()
    result = runner.run(["GATE-D3-24"])
    assert [r.gate_id for r in result.results] == ["GATE-D3-24"]


def test_the_binding_refuses_an_unusable_sha(tmp_path):
    from evaluation.binding import CommitUnavailable, resolve_head

    with pytest.raises(CommitUnavailable):
        resolve_head(tmp_path)


def test_candidate_binding_short_form():
    binding = CandidateBinding(commit_sha="a" * 40)
    assert binding.short == "a" * 12
