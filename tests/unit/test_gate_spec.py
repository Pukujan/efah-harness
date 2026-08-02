"""Gate loading and the no-model-judge refusal.

Contract Section 17.3 · ``model-policy.yaml`` ``authority_limits`` ·
GATE-D2-20 A2. The refusal happens in the loader, so a gate that admits a model
judge cannot be constructed at all -- there is no code path that obtains the
object and then forgets to check the flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.gate_spec import (
    EXPECTED_GATE_COUNT,
    GATE_DIR,
    GateSpecInvalid,
    ModelJudgeInVerdictPath,
    assert_no_model_judge,
    load_all_gates,
    load_gate,
)


def test_all_twenty_seven_visible_gates_load():
    gates = load_all_gates()
    assert len(gates) == EXPECTED_GATE_COUNT, sorted(gates)
    assert len(list(GATE_DIR.glob("GATE-*.yaml"))) == EXPECTED_GATE_COUNT


def test_every_gate_declares_no_model_judge():
    gates = load_all_gates()
    evidence = assert_no_model_judge(gates)
    assert evidence["gates_loaded"] == EXPECTED_GATE_COUNT
    assert all(
        entry["model_judge_in_verdict_path"] is False for entry in evidence["gates"].values()
    )


def test_a_gate_that_admits_a_model_judge_is_refused(tmp_path: Path):
    source = GATE_DIR / "GATE-D2-20-oracle-health-and-no-judge-in-the-determinis.yaml"
    mutated = tmp_path / source.name
    mutated.write_text(
        source.read_text().replace(
            "model_judge_in_verdict_path: false", "model_judge_in_verdict_path: true"
        )
    )
    with pytest.raises(ModelJudgeInVerdictPath):
        load_gate(mutated)


def test_a_gate_that_omits_the_flag_entirely_is_also_refused(tmp_path: Path):
    """Absence is not permission. A silent default here would void the rule."""
    source = GATE_DIR / "GATE-D3-24-known-bad-mutant-is-rejected.yaml"
    mutated = tmp_path / source.name
    mutated.write_text(
        "\n".join(
            line
            for line in source.read_text().splitlines()
            if not line.startswith("model_judge_in_verdict_path")
        )
    )
    with pytest.raises(ModelJudgeInVerdictPath):
        load_gate(mutated)


def test_a_gate_with_no_assertions_is_invalid(tmp_path: Path):
    path = tmp_path / "GATE-X.yaml"
    path.write_text(
        "gate_id: GATE-X\nmodel_judge_in_verdict_path: false\nassertions: []\n"
    )
    with pytest.raises(GateSpecInvalid):
        load_gate(path)


def test_assertion_details_survive_loading():
    gate = load_all_gates()["GATE-D1-08"]
    a5 = next(a for a in gate.assertions if a.assertion_id == "A5")
    assert a5.failure_state == "FAILED_PROVENANCE"
    assert "evaluation_request_id" in a5.raw["permitted_fields"]
    assert gate.remediation_must_not_include == "granting_builder_access_to_sealed_side"


def test_the_gate_source_hash_binds_the_file_bytes(tmp_path: Path):
    source = GATE_DIR / "GATE-D2-19-visible-hidden-and-mutant-suites-run-against.yaml"
    original = load_gate(source)
    copy = tmp_path / source.name
    copy.write_text(source.read_text() + "\n# a trailing comment\n")
    assert load_gate(copy).source_hash != original.source_hash
