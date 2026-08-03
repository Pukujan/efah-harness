"""EFAH Eval Lab task #1 — the MBPP execution-oracle lane on Inspect AI.

WHY THIS LANE
-------------
Of the 73 objective lanes under `/home/yoav/ssc-github/evals/objective_*/`, this task is
wired to `objective_mbpp`, and the choice is on three grounds:

1. **Its checker decides by EXECUTION, and execution is the strongest verdict authority
   available.** `checker_mbpp.check_record(candidate_code, test_list, test_setup_code)`
   concatenates `setup + candidate + every assert` into one source file and runs it in a
   fresh isolated interpreter (`python -I -S`, hard timeout, throwaway tempdir cwd). PASS
   iff the process exits 0. No pre-supplied label is trusted; the verdict is re-derived on
   every call. `label_authority: "subprocess_test_execution"`,
   `judge_in_verdict_path: false`.

2. **It has recorded mutation-kill calibration**, which is what makes the negatives
   trustworthy. `run_manifest.json` records 230 source problems promoted into 460 records:
   for each problem the dataset reference was executed to confirm it PASSes, then a
   deterministic single-AST-node mutation was synthesised and KEPT ONLY IF the oracle
   re-executed it to FAIL (`mutation_dist`: negate_comparison 123, return_none 47,
   off_by_one_numeric 45, swap_arith_operator 15; `mutants_needing_fallback_strategy`
   107; `quarantine` 0). A wrong answer here was never assumed wrong — it was
   execution-confirmed wrong. Balanced by construction: 230 PASS / 230 FAIL.

3. **It is not `objective_cruxeval`.** Measured 2026-08-03, the cruxeval checker reads
   ground truth from the candidate's STDOUT, so a candidate can print a marker and forge
   its own truth. MBPP's checker never reads candidate stdout for truth — it reads the
   process exit status. That is a materially harder thing to lie about, though not an
   impossible one (see the honest blind spot below, and `gold_lanes.py`).

WHAT THE CHECKER CAN AND CANNOT DECIDE
--------------------------------------
CAN: "does this code satisfy these asserts on this interpreter, right now" — assertion
failure, uncaught exception, syntax error, non-zero exit and timeout are all FAIL; an empty
`test_list` is refused as FAIL rather than granted a free green.

CANNOT: whether the code is *good* (readable, efficient, idiomatic, non-cheating); whether
it generalises beyond the 1-3 asserts MBPP ships per problem (a lookup table keyed on the
three asserted inputs passes); whether it is safe (`-I -S` isolates imports and site config,
it is NOT a sandbox — the candidate has full filesystem and network reach from inside the
"no network" harness); and, critically, whether the asserts ran at all — see below.

HONEST BLIND SPOT (found while wiring this, 2026-08-03)
-------------------------------------------------------
The verdict is `returncode == 0`, and the asserts are appended into the SAME module as the
candidate. A candidate that terminates the interpreter with status 0 at import time is
graded PASS without a single assert executing. Verified forging candidates against
`assert f(1)==2; assert f(2)==3` with `def f(x): return 999`:

    import sys; sys.exit(0)                              -> PASS
    import os; os._exit(0)                               -> PASS
    raise SystemExit(0)                                  -> PASS
    import os, atexit; atexit.register(lambda: os._exit(0))  -> PASS

The recorded 460 gold rows are NOT contaminated (dataset references + AST mutants cannot
call `sys.exit`), so this is a prospective hole that opens the moment a model solver is
attached. `gold_lanes.candidate_terminates_interpreter` is the tripwire; it reports into
`diagnostics` and sets `quarantine_reason`, and it does NOT override the checker — the
checker is the verdict, always. The durable fix (an assert-execution witness) belongs in
the lane upstream and is not this task's to make.

THE RULE
--------
`Score.value` is the deterministic checker's verdict and nothing else. Inspect's own
native scorer runs alongside it on every sample and its result is filed under
`Score.metadata["diagnostics"]["inspect_native_score"]`, explicitly labelled advisory. The
test suite proves the two can disagree and that `passed` follows the checker.

RUN IT (offline, no model, no spend)
------------------------------------
    cd /home/yoav/efah/efah-harness
    /home/yoav/efah/.venv/bin/inspect eval evals/mbpp_execution_lane.py \
        --model mockllm/model -T limit=20 --log-dir /tmp/efah-eval-logs

`mockllm/model` never leaves the process. The default solver replays the recorded
candidate and never calls `generate()`, so no provider is contacted even in principle.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    includes,
    scorer,
    stderr,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver

# `inspect eval <path>` loads this file as a standalone module (not as a package member),
# so the repo root has to be on sys.path for the sibling import below to resolve. Under
# pytest the root is already there and this is a no-op.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.gold_lanes import (  # noqa: E402
    IntegrityFinding,
    LaneUnavailable,
    candidate_terminates_interpreter,
    lane_dir,
    load_checker_module,
    load_gold_records,
)

LANE = "objective_mbpp"
CHECKER_FILENAME = "checker_mbpp.py"
GOLD_FILENAME = "hard_gold.jsonl"

# Mirrors checker_mbpp.TIMEOUT_S. Kept explicit so a slow host can raise it from the CLI
# without editing the upstream lane.
DEFAULT_TIMEOUT_S = 10.0

LANE_GUARANTEES = {
    "lane": LANE,
    "label_authority": "subprocess_test_execution",
    "judge_in_verdict_path": False,
    "provenance_tier": "hard_gold",
    "verdict_owner": "cortex_lane_checker",
    "inspect_role": "runner_and_logger_only",
}


def checker_path() -> Path:
    return lane_dir(LANE) / CHECKER_FILENAME


def gold_path() -> Path:
    return lane_dir(LANE) / GOLD_FILENAME


def load_checker() -> Any:
    """The deterministic oracle. Loaded from the lane's own source — never reimplemented."""
    return load_checker_module(checker_path(), "efah_evals_checker_mbpp")


# --------------------------------------------------------------------------- verdict path
def verdict_of_record(
    candidate_code: str,
    test_list: list[str],
    test_setup_code: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """THE verdict. One function, called by the Inspect scorer and by the tests alike.

    There is exactly one place in this package where a pass/fail is decided, and it is a
    call into the lane's own checker. Nothing here inspects a model, a completion string, a
    recorded label, or an Inspect `Score`.
    """
    checker = load_checker()
    result = checker.check_record(
        candidate_code, list(test_list or []), test_setup_code or "", timeout=timeout_s
    )
    label = result.objective_label
    return {
        "objective_label": label,
        "passed": label == "PASS",
        "detail": (result.detail or "")[:400],
        "label_authority": LANE_GUARANTEES["label_authority"],
    }


def integrity_report(candidate_code: str, passed: bool) -> dict[str, Any]:
    """Advisory integrity scan. Never touches the verdict; may set a quarantine reason."""
    findings: list[IntegrityFinding] = candidate_terminates_interpreter(candidate_code)
    exit_forgery = [f for f in findings if f.kind in {"process_exit_call", "atexit_register"}]
    report: dict[str, Any] = {
        "findings": [{"kind": f.kind, "detail": f.detail} for f in findings],
        "exit_code_forgery_risk": bool(exit_forgery),
    }
    if exit_forgery and passed:
        # A green verdict the guard cannot vouch for. Flagged, not flipped — the checker
        # still owns `passed`; this makes the result visibly undecidable rather than
        # silently trusted.
        report["quarantine_reason"] = "pass_may_be_forged_by_process_exit"
    return report


# ------------------------------------------------------------------------------ dataset
def _sample_from_record(record: dict, index: int) -> Sample:
    return Sample(
        input=record["prompt"],
        # The gold label. Used ONLY for parity reporting and as the advisory native
        # scorer's target — never as the verdict.
        target=record["objective_label"],
        id=str(record.get("task_id") or f"{LANE}_{index}"),
        metadata={
            "candidate_code": record["candidate_code"],
            "test_list": record["test_list"],
            "test_setup_code": record.get("test_setup_code", "") or "",
            "candidate_origin": record.get("candidate_origin"),
            "source_task_id": record.get("source_task_id"),
            "gold_objective_label": record["objective_label"],
            "label_authority": record.get("label_authority"),
            "provenance_tier": record.get("provenance_tier"),
        },
    )


def load_dataset(limit: int | None = None) -> MemoryDataset:
    """Build the Inspect dataset from the lane's `hard_gold.jsonl`.

    Raises `LaneUnavailable` rather than returning an empty dataset — an eval that grades
    zero samples and reports a clean run is a false green.
    """
    records = load_gold_records(gold_path(), limit=limit)
    samples = [_sample_from_record(r, i) for i, r in enumerate(records)]
    return MemoryDataset(samples, name=LANE)


# ------------------------------------------------------------------------- solver seam
@solver
def replay_recorded_candidate() -> Solver:
    """The offline fake solver: emit the recorded candidate as the model output.

    This is the injectable seam. It costs nothing, contacts nothing, and is fully
    deterministic, so the whole plumbing — dataset -> solver -> deterministic scorer ->
    Inspect log — is provable without a model. Swapping in `inspect_ai.solver.generate()`
    (or any agent) changes nothing downstream: the scorer grades whatever string lands in
    `state.output.completion`, from any source.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        code = state.metadata["candidate_code"]
        state.output = ModelOutput.from_content(model="replay/recorded_candidate", content=code)
        return state

    return solve


@solver
def fixed_candidate(code: str, label: str = "fixture") -> Solver:
    """A solver that emits one caller-supplied candidate for every sample.

    Used by the tests to drive deliberately-wrong and known-good candidates through the
    real Inspect pipeline, so what is proven is the shipped scorer and not a stub of it.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.output = ModelOutput.from_content(model=f"fixture/{label}", content=code)
        return state

    return solve


# ------------------------------------------------------------------------------ scorer
@scorer(metrics=[accuracy(), stderr()])
def execution_oracle_scorer(timeout_s: float = DEFAULT_TIMEOUT_S) -> Scorer:
    """Grade by executing the candidate against the lane's asserts. Nothing else grades.

    `Score.value` is CORRECT iff the lane checker returned PASS. Inspect's own native
    scorer (`includes()`) is computed on the same sample and filed under
    `metadata["diagnostics"]["inspect_native_score"]` — advisory, recorded so it can be
    compared and audited, structurally unable to move `Score.value`.
    """
    native_advisory = includes()

    async def score(state: TaskState, target: Target) -> Score:
        candidate = state.output.completion if state.output else ""
        meta = state.metadata

        # 1. THE VERDICT — deterministic execution, off the event loop so a 10s subprocess
        #    timeout does not stall Inspect's other samples.
        verdict = await asyncio.to_thread(
            verdict_of_record,
            candidate,
            meta["test_list"],
            meta.get("test_setup_code", ""),
            timeout_s,
        )
        passed = verdict["passed"]

        # 2. ADVISORY ONLY — inspect's native scorer. It can and does disagree; it is
        #    recorded, never consulted.
        try:
            native = await native_advisory(state, target)
            native_value: Any = native.value
        except Exception as exc:
            native_value = f"<native scorer error: {exc!r}>"

        gold = meta.get("gold_objective_label")
        integrity = integrity_report(candidate, passed)

        diagnostics: dict[str, Any] = {
            "advisory_only": True,
            "inspect_native_score": native_value,
            "inspect_native_scorer": "includes()",
            "note": (
                "inspect_native_score is a framework-native text match with NO authority "
                "over `passed`; the lane checker's execution verdict is the verdict of record"
            ),
            "integrity": integrity,
            "solver_source": (state.output.model if state.output else None),
            "candidate_origin": meta.get("candidate_origin"),
            **LANE_GUARANTEES,
        }
        if "quarantine_reason" in integrity:
            diagnostics["quarantine_reason"] = integrity["quarantine_reason"]

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=verdict["objective_label"],
            explanation=(
                f"checker={verdict['objective_label']} gold={gold} "
                f"parity={verdict['objective_label'] == gold} "
                f"detail={verdict['detail'][:160]!r}"
            ),
            metadata={
                "passed": passed,
                "checker_verdict": verdict["objective_label"],
                "verdict_authority": verdict["label_authority"],
                "gold_objective_label": gold,
                # Reproduction of the recorded label. Reported, not graded — this task
                # grades candidates, and a candidate that legitimately disagrees with a
                # stale gold label must not be scored on the label.
                "parity_with_gold": verdict["objective_label"] == gold,
                "detail": verdict["detail"],
                "diagnostics": diagnostics,
            },
        )

    return score


# -------------------------------------------------------------------------------- task
@task
def mbpp_execution_lane(
    limit: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    solver: Solver | None = None,
) -> Task:
    """MBPP execution-oracle lane. `solver` is injectable; the default runs fully offline.

    Args:
        limit: grade only the first N gold records (CLI: `-T limit=20`). None = all 460.
        timeout_s: per-candidate subprocess timeout handed to the lane checker.
        solver: any Inspect solver. Defaults to `replay_recorded_candidate()`, which needs
            no model and no network. Pass `inspect_ai.solver.generate()` to grade a real
            model through the identical deterministic scorer.
    """
    return Task(
        dataset=load_dataset(limit=limit),
        solver=solver or replay_recorded_candidate(),
        scorer=execution_oracle_scorer(timeout_s=timeout_s),
        name=LANE,
        metadata=dict(LANE_GUARANTEES),
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "LANE",
    "LANE_GUARANTEES",
    "LaneUnavailable",
    "execution_oracle_scorer",
    "fixed_candidate",
    "load_checker",
    "load_dataset",
    "mbpp_execution_lane",
    "replay_recorded_candidate",
    "verdict_of_record",
]
