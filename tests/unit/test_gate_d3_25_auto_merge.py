"""The thirteen auto-merge requirements as one composite.

Contract Section 21.2 · GATE-D3-25. Two failure modes are tested in equal
measure: a PR that merges when it should not, and a green PR that waits for a
human it should not need. The contract forbids both.
"""

from __future__ import annotations

import pytest

from evaluation.auto_merge import (
    AUTO_MERGE_REQUIREMENTS,
    REQUIRED_VALUES,
    AutoMergeEvaluation,
    RequirementNotEvaluated,
)
from evaluation.binding import CandidateBinding, EvaluationSet, Lane, LaneRun
from governance.states import Verdict


def _green(**overrides) -> AutoMergeEvaluation:
    evaluation = AutoMergeEvaluation(
        pull_request_ref="PR-1",
        candidate_commit="a" * 40,
        implementing_agent_alias="implementer-i12",
        merge_actor="github-actions[bot]",
        ci_checks_present=True,
    )
    for name in AUTO_MERGE_REQUIREMENTS:
        evaluation.record(name, REQUIRED_VALUES[name], source="test")
    for name, value in overrides.items():
        if name in REQUIRED_VALUES:
            evaluation.record(name, value, source="test-override")
        else:
            setattr(evaluation, name, value)
    return evaluation


def test_there_are_exactly_thirteen_requirements():
    assert len(AUTO_MERGE_REQUIREMENTS) == 13
    assert len(REQUIRED_VALUES) == 13
    assert set(AUTO_MERGE_REQUIREMENTS) == set(REQUIRED_VALUES)


def test_a_fully_green_pr_merges_without_a_human_message():
    evaluation = _green()
    allowed, blockers = evaluation.may_merge()
    assert allowed, blockers
    assert evaluation.verdict() is Verdict.PASS
    assert evaluation.as_evidence()["waits_for_human_message"] is False


def test_all_thirteen_are_recorded_per_pr():
    evidence = _green().as_evidence()
    assert evidence["all_thirteen_recorded"] is True
    assert len(evidence["requirements"]) == 13
    assert evidence["never_evaluated"] == []


@pytest.mark.parametrize("requirement", AUTO_MERGE_REQUIREMENTS)
def test_failing_any_single_requirement_blocks_the_merge(requirement):
    required = REQUIRED_VALUES[requirement]
    if isinstance(required, bool):
        bad_value = not required
    elif isinstance(required, int):
        bad_value = required + 3
    else:
        bad_value = "FAIL"
    evaluation = _green(**{requirement: bad_value})
    allowed, blockers = evaluation.may_merge()
    assert not allowed
    assert evaluation.verdict() is Verdict.FAIL
    assert any(requirement in b for b in blockers)


@pytest.mark.parametrize("omitted", AUTO_MERGE_REQUIREMENTS)
def test_an_unevaluated_requirement_is_not_a_satisfied_one(omitted):
    evaluation = AutoMergeEvaluation(
        pull_request_ref="PR-1",
        candidate_commit="a" * 40,
        merge_actor="ci",
        ci_checks_present=True,
    )
    for name in AUTO_MERGE_REQUIREMENTS:
        if name != omitted:
            evaluation.record(name, REQUIRED_VALUES[name], source="test")
    assert evaluation.missing == [omitted]
    with pytest.raises(RequirementNotEvaluated):
        evaluation.verdict()
    allowed, _ = evaluation.may_merge()
    assert not allowed


def test_not_evaluated_is_a_third_state_distinct_from_pass_and_fail():
    evaluation = _green()
    evaluation.record_not_evaluated("hidden_holdout", "sealed side unavailable")
    assert evaluation.all_thirteen_recorded
    assert evaluation.not_evaluated == ["hidden_holdout"]
    assert evaluation.verdict() is Verdict.UNVERIFIABLE
    allowed, blockers = evaluation.may_merge()
    assert not allowed
    assert any("not evaluated" in b for b in blockers)


def test_the_implementing_agent_may_not_merge_its_own_work():
    evaluation = _green(merge_actor="implementer-i12")
    allowed, blockers = evaluation.may_merge()
    assert not allowed
    assert any("implementing agent" in b for b in blockers)


def test_a_pr_with_no_ci_checks_is_unmeasured_not_green():
    evaluation = _green(ci_checks_present=False)
    allowed, blockers = evaluation.may_merge()
    assert not allowed
    assert any("no CI checks" in b for b in blockers)


def test_an_unknown_requirement_name_is_refused():
    evaluation = AutoMergeEvaluation(pull_request_ref="PR-1", candidate_commit="a" * 40)
    with pytest.raises(RequirementNotEvaluated):
        evaluation.record("looks_good_to_me", True, source="nowhere")


def test_the_record_is_a_sealed_compiled_object():
    obj = _green().to_compiled_object()
    assert obj.is_intact()
    assert obj.body["requirement_count"] == 13


# --- GATE-D2-19: one commit across every lane -----------------------------

def test_lanes_must_agree_on_the_candidate_commit():
    binding = CandidateBinding(commit_sha="a" * 40)
    evaluation_set = EvaluationSet(evaluation_request_id="EVAL-1", binding=binding)
    for lane in Lane:
        evaluation_set.record(LaneRun(lane, binding.commit_sha, Verdict.PASS))
    assert evaluation_set.lanes_agree
    assert evaluation_set.complete
    assert evaluation_set.verdict() is Verdict.PASS


def test_a_commit_change_between_lanes_invalidates_the_set():
    binding = CandidateBinding(commit_sha="a" * 40)
    evaluation_set = EvaluationSet(evaluation_request_id="EVAL-1", binding=binding)
    evaluation_set.record(LaneRun(Lane.VISIBLE, "a" * 40, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.MUTANT, "a" * 40, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.HIDDEN, "b" * 40, Verdict.PASS))
    assert evaluation_set.invalidated
    assert evaluation_set.verdict() is Verdict.FAIL


def test_an_incomplete_set_is_unverifiable_not_passing():
    binding = CandidateBinding(commit_sha="a" * 40)
    evaluation_set = EvaluationSet(evaluation_request_id="EVAL-1", binding=binding)
    evaluation_set.record(LaneRun(Lane.VISIBLE, "a" * 40, Verdict.PASS))
    assert not evaluation_set.complete
    assert evaluation_set.verdict() is Verdict.UNVERIFIABLE
