"""Proof that EFAH's first Inspect AI task is wired to a deterministic verdict.

Four claims, each with a test that would fail if the claim were false:

  (a) the task loads the lane's records (460, balanced 230/230)
  (b) a deliberately WRONG candidate scores fail
  (c) a known-good candidate scores pass
  (d) the verdict comes from the lane's deterministic checker and not from any model —
      proved three ways: the recorded gold label is provably not consulted; Inspect's own
      native score is shown DISAGREEING with `passed` on a candidate crafted to fool it;
      and the whole verdict path is AST-scanned for judge/LLM/network imports.

Plus a fifth: a live regression test for the exit-code forgery blind spot found while
wiring this, so it is a measurement in CI rather than a paragraph in a doc.

Everything here is offline. `mockllm/model` is a local stub and the solvers never call
`generate()`, so no provider is contacted and nothing is billed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.gold_lanes import (
    LaneUnavailable,
    candidate_terminates_interpreter,
    load_gold_records,
    verdict_path_is_judge_free,
)
from evals.mbpp_execution_lane import (
    LANE_GUARANTEES,
    checker_path,
    fixed_candidate,
    gold_path,
    integrity_report,
    load_checker,
    load_dataset,
    mbpp_execution_lane,
    verdict_of_record,
)
from inspect_ai import eval as inspect_eval
from inspect_ai.scorer import CORRECT, INCORRECT

from evals import gold_lanes

EXPECTED_GOLD_ROWS = 460
EXPECTED_LABEL_DIST = {"PASS": 230, "FAIL": 230}
MOCK_MODEL = "mockllm/model"

# A host without the Cortex gold tree gets one clear skip reason for the whole module
# rather than a cascade of errors.
@pytest.fixture(scope="module", autouse=True)
def _require_lane():
    try:
        gold_path()
        checker_path()
    except LaneUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def gold_by_id() -> dict[str, dict]:
    return {r["task_id"]: r for r in load_gold_records(gold_path())}


def _run(task, tmp_path: Path):
    """Run one Inspect eval offline and return its log."""
    logs = inspect_eval(
        task,
        model=MOCK_MODEL,
        display="none",
        log_dir=str(tmp_path / "logs"),
        score_display=False,
    )
    assert logs[0].status == "success", logs[0].error
    return logs[0]


def _sample_scores(log):
    return [(s.id, s.scores["execution_oracle_scorer"]) for s in log.samples]


# ---------------------------------------------------------------- (a) the task loads N
def test_a_dataset_loads_all_gold_records():
    dataset = load_dataset()
    assert len(dataset) == EXPECTED_GOLD_ROWS

    dist: dict[str, int] = {}
    for sample in dataset:
        dist[sample.target] = dist.get(sample.target, 0) + 1
    assert dist == EXPECTED_LABEL_DIST, "lane is no longer balanced 230 PASS / 230 FAIL"

    first = dataset[0]
    assert first.metadata["candidate_code"]
    assert first.metadata["test_list"], "a sample with no asserts is not gradable"


def test_a_task_dataset_honours_limit():
    assert len(mbpp_execution_lane(limit=8).dataset) == 8


def test_a_empty_or_missing_lane_refuses_rather_than_reporting_zero(monkeypatch, tmp_path):
    """A lane that cannot be found must raise, never yield an empty 100%-clean run."""
    monkeypatch.setenv("EFAH_GOLD_LANE_ROOT", str(tmp_path))
    with pytest.raises(LaneUnavailable):
        load_dataset()


# ----------------------------------------------- (b)/(c) wrong fails, known-good passes
def test_c_known_good_candidate_scores_pass(tmp_path, gold_by_id):
    """The recorded MBPP reference solution, replayed, executes to PASS."""
    record = gold_by_id["mbpp_11__pass"]
    task = mbpp_execution_lane(limit=1)
    task.dataset = load_dataset(limit=2).filter(lambda s: s.id == "mbpp_11__pass")
    log = _run(task, tmp_path)

    (sid, score), = _sample_scores(log)
    assert sid == "mbpp_11__pass"
    assert score.value == CORRECT
    assert score.answer == "PASS"
    assert score.metadata["passed"] is True
    assert score.metadata["parity_with_gold"] is True
    assert record["objective_label"] == "PASS"


def test_b_recorded_mutant_scores_fail(tmp_path):
    """The execution-verified mutant twin, replayed, executes to FAIL."""
    task = mbpp_execution_lane()
    task.dataset = load_dataset().filter(lambda s: s.id == "mbpp_11__fail")
    log = _run(task, tmp_path)

    (sid, score), = _sample_scores(log)
    assert sid == "mbpp_11__fail"
    assert score.value == INCORRECT
    assert score.answer == "FAIL"
    assert score.metadata["passed"] is False
    assert "AssertionError" in score.metadata["detail"] or score.metadata["detail"]


def test_b_hand_broken_candidate_scores_fail(tmp_path):
    """A deliberately wrong candidate injected over a PASS sample still scores fail.

    Same sample, same asserts, same scorer as `test_c_...` — only the candidate changed. If
    the scorer were reading the recorded gold label (which says PASS) instead of executing,
    this test would go green and the whole lab would be theatre.
    """
    broken = 'def remove_Occ(s, ch):\n    return s  # deliberately wrong: removes nothing\n'
    task = mbpp_execution_lane(solver=fixed_candidate(broken, label="deliberately_wrong"))
    task.dataset = load_dataset().filter(lambda s: s.id == "mbpp_11__pass")
    log = _run(task, tmp_path)

    (_, score), = _sample_scores(log)
    assert score.value == INCORRECT
    assert score.answer == "FAIL"
    assert score.metadata["gold_objective_label"] == "PASS"
    assert score.metadata["parity_with_gold"] is False, (
        "the gold label says PASS and the candidate genuinely fails — the scorer must "
        "report the execution verdict, not the label"
    )


def test_bc_balanced_replay_batch_reproduces_recorded_labels(tmp_path):
    """A 12-sample replay: every recorded label is reproduced by re-execution."""
    task = mbpp_execution_lane(limit=12)
    log = _run(task, tmp_path)

    scores = _sample_scores(log)
    assert len(scores) == 12
    verdicts = {sid: s.answer for sid, s in scores}
    assert sum(v == "PASS" for v in verdicts.values()) == 6
    assert sum(v == "FAIL" for v in verdicts.values()) == 6
    for sid, s in scores:
        assert s.metadata["parity_with_gold"] is True, f"{sid} did not reproduce its label"
        assert s.metadata["verdict_authority"] == "subprocess_test_execution"


# ------------------------------------------- (d) the verdict is the checker's, not a model's
def test_d_verdict_ignores_the_recorded_gold_label(gold_by_id):
    """`verdict_of_record` never sees a label. Corrupting it changes nothing."""
    good = gold_by_id["mbpp_11__pass"]
    bad = gold_by_id["mbpp_11__fail"]

    assert verdict_of_record(good["candidate_code"], good["test_list"])["objective_label"] == "PASS"
    assert verdict_of_record(bad["candidate_code"], bad["test_list"])["objective_label"] == "FAIL"

    # Swap the candidates between the twins' recorded labels: the verdicts follow the CODE.
    assert verdict_of_record(bad["candidate_code"], good["test_list"])["objective_label"] == "FAIL"
    assert verdict_of_record(good["candidate_code"], bad["test_list"])["objective_label"] == "PASS"


def test_d_inspect_native_score_disagrees_and_has_no_authority(tmp_path):
    """The load-bearing test: Inspect's own scorer says CORRECT, `passed` says False.

    The candidate is functionally wrong but contains the literal string "PASS", which is
    the sample's target — so inspect's native `includes()` matches and returns CORRECT. It
    is recorded under `diagnostics.inspect_native_score` and it does not move the verdict.
    """
    liar = (
        'def remove_Occ(s, ch):\n'
        '    return "PASS"  # advisory scorers match this; execution does not care\n'
    )
    task = mbpp_execution_lane(solver=fixed_candidate(liar, label="native_score_bait"))
    task.dataset = load_dataset().filter(lambda s: s.id == "mbpp_11__pass")
    log = _run(task, tmp_path)

    (_, score), = _sample_scores(log)
    diagnostics = score.metadata["diagnostics"]

    assert diagnostics["inspect_native_score"] == CORRECT, (
        "precondition: inspect's native includes() must MATCH here, otherwise this test "
        "proves nothing about precedence"
    )
    assert score.value == INCORRECT
    assert score.metadata["passed"] is False
    assert diagnostics["advisory_only"] is True
    assert diagnostics["judge_in_verdict_path"] is False
    assert diagnostics["inspect_role"] == "runner_and_logger_only"


def test_d_verdict_path_has_no_judge_llm_or_network_import():
    """Structural AST scan of every file that can influence `passed`."""
    verdict_modules = [
        Path(gold_lanes.__file__),
        Path(__import__("evals.mbpp_execution_lane", fromlist=["x"]).__file__),
        checker_path(),
    ]
    clean, problems = verdict_path_is_judge_free(verdict_modules)
    assert clean, problems


def test_d_forbidden_import_list_matches_upstream_oracle_adapter():
    """Guard against drift from `/home/yoav/ssc-github/evals/oracle_adapter.py`."""
    upstream = gold_lanes.gold_lane_root() / "oracle_adapter.py"
    if not upstream.is_file():
        pytest.skip("upstream oracle_adapter.py not present on this host")
    ns: dict = {}
    exec(compile(upstream.read_text(encoding="utf-8-sig"), str(upstream), "exec"), ns)
    assert tuple(ns["FORBIDDEN_VERDICT_IMPORT_PREFIXES"]) == (
        gold_lanes.FORBIDDEN_VERDICT_IMPORT_PREFIXES
    )


def test_d_lane_declares_no_judge():
    assert LANE_GUARANTEES["judge_in_verdict_path"] is False
    assert LANE_GUARANTEES["label_authority"] == "subprocess_test_execution"
    assert LANE_GUARANTEES["verdict_owner"] == "cortex_lane_checker"


# ------------------------------------------------ blind spot: exit-code forgery (measured)
FORGING_CANDIDATES = {
    "sys_exit": "import sys\nsys.exit(0)\ndef remove_Occ(s, ch):\n    return 'nonsense'\n",
    "os_exit": "import os\nos._exit(0)\ndef remove_Occ(s, ch):\n    return 'nonsense'\n",
    "raise_systemexit": "raise SystemExit(0)\n",
    "atexit": (
        "import os, atexit\natexit.register(lambda: os._exit(0))\n"
        "def remove_Occ(s, ch):\n    return 'nonsense'\n"
    ),
}


@pytest.mark.parametrize("name,code", sorted(FORGING_CANDIDATES.items()))
def test_blind_spot_checker_grades_exit_forgers_as_pass(name, code, gold_by_id):
    """MEASUREMENT, not an endorsement.

    The lane checker's verdict is `returncode == 0` and the asserts share a module with the
    candidate, so a candidate that exits 0 at import time is graded PASS having executed no
    assert at all. This test pins that behaviour so the day the lane is fixed upstream, it
    goes red and this package is forced to notice.
    """
    asserts = gold_by_id["mbpp_11__pass"]["test_list"]
    verdict = verdict_of_record(code, asserts)
    assert verdict["objective_label"] == "PASS", (
        "blind spot appears to be FIXED upstream — update evals/gold_lanes.py and the "
        "module docstrings, this is good news"
    )


@pytest.mark.parametrize("name,code", sorted(FORGING_CANDIDATES.items()))
def test_blind_spot_guard_detects_every_known_forger(name, code):
    findings = candidate_terminates_interpreter(code)
    assert findings, f"{name} slipped past the exit-code forgery guard"
    report = integrity_report(code, passed=True)
    assert report["exit_code_forgery_risk"] is True
    assert report["quarantine_reason"] == "pass_may_be_forged_by_process_exit"


def test_blind_spot_guard_is_quiet_on_honest_gold(gold_by_id):
    """No false positives across the whole lane: 460/460 recorded candidates are clean."""
    noisy = [
        r["task_id"]
        for r in load_gold_records(gold_path())
        if candidate_terminates_interpreter(r["candidate_code"])
    ]
    assert noisy == [], f"guard fired on recorded gold: {noisy[:5]}"
    assert integrity_report(gold_by_id["mbpp_11__pass"]["candidate_code"], passed=True) == {
        "findings": [],
        "exit_code_forgery_risk": False,
    }


# -------------------------------------------------------------------------- plumbing bits
def test_checker_is_loaded_from_the_lane_not_reimplemented():
    """The oracle is the lane's own file; this package never re-implements a checker."""
    checker = load_checker()
    assert Path(checker.__file__) == checker_path()
    assert hasattr(checker, "check_record")
    # `dataclasses` needs the module in sys.modules during exec; a second load must not
    # re-execute or the identity check above becomes meaningless.
    assert load_checker() is checker


def test_eval_log_is_written_and_records_the_guarantees(tmp_path):
    log = _run(mbpp_execution_lane(limit=2), tmp_path)
    assert log.eval.model == MOCK_MODEL
    files = list((tmp_path / "logs").glob("*.eval"))
    assert files, "inspect wrote no eval log"
    payload = json.dumps(log.eval.metadata or {})
    assert "subprocess_test_execution" in payload
