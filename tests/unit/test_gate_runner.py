"""The gate runner's own rules.

Contract Sections 17, 18, 25. These tests are about the runner's honesty, not
about any single gate: that it never reports PASS for a gate it only partly
executed, never reports PASS without the evidence the gate named, and treats a
check that raises as a failure rather than skipping it.

Every one of those is a way a gate suite quietly becomes decorative.
"""

from __future__ import annotations

from dataclasses import replace

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


# --- the store's read side ------------------------------------------------
#
# GATE-D1-10 ran 10/10 assertions PASS, EXECUTED, and still reported four named
# artifacts missing while the files were sitting in evidence/gates/GATE-D1-10/.
# The store was write-only: `missing_for` answered from the in-memory dict that
# `add` fills during the run, and nothing ever read the directory back. Some
# evidence cannot be produced any other way -- a live service, a browser, a host
# CI does not have -- so a gate whose evidence comes from a collector could
# never be satisfied by anything.
#
# The tests below pin both halves. Adoption has to work, and it has to refuse:
# an artifact from another commit is evidence about a different program, and
# admitting one is how a gate turns green on a build nobody ran.


def _envelope(gate_id: str, name: str, commit: str) -> dict:
    return {"artifact": name, "gate_id": gate_id, "candidate_commit": commit, "contents": {"ok": True}}


def test_an_artifact_written_at_this_commit_is_adopted_from_disk(tmp_path):
    import json

    commit = "b" * 40
    store = EvidenceStore(candidate_commit=commit, root=tmp_path)
    directory = tmp_path / "GATE-X"
    directory.mkdir()
    (directory / "collected_transcript.json").write_text(
        json.dumps(_envelope("GATE-X", "collected_transcript", commit))
    )

    assert store.missing_for("GATE-X", ("collected_transcript",)) == ["collected_transcript"]
    adopted = store.adopt_from_disk("GATE-X")

    assert [a.name for a in adopted] == ["collected_transcript"]
    assert store.missing_for("GATE-X", ("collected_transcript",)) == []
    assert store.references_for("GATE-X")[0]["candidate_commit"] == commit
    assert store.refused_for("GATE-X") == []


def test_an_artifact_from_another_commit_is_refused_not_adopted(tmp_path):
    import json

    store = EvidenceStore(candidate_commit="b" * 40, root=tmp_path)
    directory = tmp_path / "GATE-X"
    directory.mkdir()
    (directory / "collected_transcript.json").write_text(
        json.dumps(_envelope("GATE-X", "collected_transcript", "c" * 40))
    )

    assert store.adopt_from_disk("GATE-X") == []
    assert store.missing_for("GATE-X", ("collected_transcript",)) == ["collected_transcript"]
    refused = store.refused_for("GATE-X")
    assert len(refused) == 1
    assert "bound to candidate commit cccccccccccc" in refused[0]["reason"]


def test_a_file_with_no_commit_binding_is_refused(tmp_path):
    """A bare payload is not evidence: nothing says which build it describes."""
    import json

    store = EvidenceStore(candidate_commit="b" * 40, root=tmp_path)
    directory = tmp_path / "GATE-X"
    directory.mkdir()
    # Exactly the shape tools/collect_d1_10_evidence.py writes: the payload
    # only, with no artifact name, gate id or candidate commit around it.
    (directory / "collected_transcript.json").write_text(json.dumps({"viewport": "390x844"}))

    assert store.adopt_from_disk("GATE-X") == []
    assert store.missing_for("GATE-X", ("collected_transcript",)) == ["collected_transcript"]
    reason = store.refused_for("GATE-X")[0]["reason"]
    assert "candidate_commit" in reason


def test_disk_never_overwrites_an_artifact_produced_in_this_run(tmp_path):
    import json

    commit = "b" * 40
    store = EvidenceStore(candidate_commit=commit, root=tmp_path)
    live = store.add("GATE-X", "collected_transcript", {"from": "this run"})
    directory = tmp_path / "GATE-X"
    directory.mkdir()
    (directory / "collected_transcript.json").write_text(
        json.dumps(_envelope("GATE-X", "collected_transcript", commit))
    )

    store.adopt_from_disk("GATE-X")
    assert store.artifacts[("GATE-X", "collected_transcript")].content_hash == live.content_hash


def test_the_runner_consults_the_store_on_disk(tmp_path):
    """The mechanism gap itself, at the level the symptom appeared.

    Before the read side existed this failed: the runner emitted its evidence,
    called `missing_for`, and never looked at the directory -- so an artifact
    a collector had already written was reported missing.
    """
    import json

    runner = GateRunner()
    base = runner.gates["GATE-D2-20"]
    commit = runner.binding.commit_sha

    store = EvidenceStore(candidate_commit=commit, root=tmp_path)
    directory = tmp_path / base.gate_id
    directory.mkdir()
    extra = "an_artifact_only_a_collector_can_make"
    (directory / f"{extra}.json").write_text(json.dumps(_envelope(base.gate_id, extra, commit)))
    gate = replace(base, evidence_required=(*base.evidence_required, extra))

    result = GateRunner(binding=runner.binding, evidence_store=store).run_gate(gate)

    assert extra not in result.evidence_missing, result.evidence_missing
    assert extra in {a["name"] for a in result.evidence_produced}


def test_stale_on_disk_evidence_is_reported_not_silently_missing(tmp_path):
    """Refusing is not the same as not looking, and the result has to say so."""
    import json

    runner = GateRunner()
    base = runner.gates["GATE-D2-20"]

    store = EvidenceStore(candidate_commit=runner.binding.commit_sha, root=tmp_path)
    directory = tmp_path / base.gate_id
    directory.mkdir()
    extra = "an_artifact_only_a_collector_can_make"
    (directory / f"{extra}.json").write_text(json.dumps(_envelope(base.gate_id, extra, "d" * 40)))
    gate = replace(base, evidence_required=(*base.evidence_required, extra))

    result = GateRunner(binding=runner.binding, evidence_store=store).run_gate(gate)

    assert result.verdict is Verdict.UNVERIFIABLE
    assert extra in result.evidence_missing
    assert [r["name"] for r in result.evidence_refused] == [extra]
    assert "dddddddddddd" in result.evidence_refused[0]["reason"]
