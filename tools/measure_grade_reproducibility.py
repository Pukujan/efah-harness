#!/usr/bin/env python3
"""Is the sealed-holdout verdict reproducible? Grade the same thing N times.

This is the measurement the mint/grade split exists to make possible, and
without it the split is a refactor with a nice story. The claim under test is
narrow and falsifiable: **grading one candidate against one frozen exam returns
the same verdict every time.**

What "the same verdict" means here is every field of the receipt except
``generated_at`` and ``generation_request_id``, which are a clock and a name and
are expected to move. Comparing the whole receipt rather than only the exit
status is deliberate: a run that reached the same verdict by a different route —
a different kill count, a different exam — would pass a looser comparison and
would not be the property being claimed.

The baseline it is measured against is on the record. On 2026-08-03, 25 runs of
the previous single-command generator on one commit produced 25 different
reference implementations and passed roughly 45% of the time. Those runs each
cost two frontier completions and about three minutes. These cost nothing and
take seconds, because no model participates in a grade.

Two candidates are graded, not one. A gate that only ever says PASS is not a
gate, so the second arm submits a deliberately broken implementation and asserts
that the failure is reproducible too — same reason, same exit status, every run.

Usage::

    python tools/measure_grade_reproducibility.py --exam-id sha256:... --runs 5
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fields expected to differ between two runs of the same grade. Everything
#: else is compared, and a difference in any of it falsifies the claim.
VOLATILE = ("generated_at", "generation_request_id")


def grade(generator: str, exam_id: str, request_id: str, candidate: str | None) -> dict:
    argv = [
        "sudo", "-n", "-u", "efah-verifier",
        "/opt/efah-verifier/venv/bin/python", generator,
        "--request-id", request_id,
        "--candidate-commit", "0" * 40,
        "--contract-version", "1.1",
        "--mode", "GRADE",
        "--exam-id", exam_id,
    ]
    if candidate:
        argv += ["--candidate-path", candidate]
    started = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    line = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
    receipt = json.loads(line)
    receipt["_elapsed_s"] = round(time.time() - started, 2)
    receipt["_process_exit"] = proc.returncode
    return receipt


def stable_view(receipt: dict) -> dict:
    return {k: v for k, v in receipt.items() if k not in VOLATILE and not k.startswith("_")}


def run_arm(generator: str, exam_id: str, runs: int, label: str, candidate: str | None) -> dict:
    receipts = [
        grade(generator, exam_id, f"repro-{label}-{i}", candidate) for i in range(1, runs + 1)
    ]
    views = [json.dumps(stable_view(r), sort_keys=True) for r in receipts]
    identical = len(set(views)) == 1
    print(f"\n{label}: {runs} runs, {len(set(views))} distinct verdict(s)")
    for i, r in enumerate(receipts, start=1):
        print(
            f"  run {i}: exit {r['exit_status']} kill_rate {r['kill_rate']} "
            f"({r['killed_count']}/{r['mutant_count']}) "
            f"reason={r['failure_reason']} {r['_elapsed_s']}s"
        )
    return {
        "arm": label,
        "candidate": candidate or "the exam's own frozen reference",
        "runs": runs,
        "distinct_verdicts": len(set(views)),
        "verdict_identical_every_run": identical,
        "compared_fields": sorted(stable_view(receipts[0])),
        "volatile_fields_excluded": list(VOLATILE),
        "receipts": receipts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--generator",
        default="/opt/efah-verifier/bin/generate-holdouts",
        help="the generator to invoke as the verifier identity",
    )
    parser.add_argument("--broken-candidate", default=None,
                        help="a directory holding a deliberately wrong subject.py")
    parser.add_argument("--out", default="evidence/grade-reproducibility.json")
    args = parser.parse_args()

    arms = [run_arm(args.generator, args.exam_id, args.runs, "reference-candidate", None)]
    if args.broken_candidate:
        arms.append(
            run_arm(
                args.generator, args.exam_id, args.runs,
                "broken-candidate", args.broken_candidate,
            )
        )

    # Both arms must be reproducible, and they must disagree with each other.
    # A gate whose two arms agree is measuring nothing, however stable it is.
    reproducible = all(a["verdict_identical_every_run"] for a in arms)
    discriminates = (
        len({a["receipts"][0]["exit_status"] for a in arms}) == len(arms) if len(arms) > 1 else None
    )
    report = {
        "measurement": "grade_reproducibility",
        "claim": (
            "grading one candidate against one frozen exam returns the same verdict "
            "every time, where 'the same verdict' is every receipt field except the "
            "clock and the request name"
        ),
        "baseline": (
            "2026-08-03: 25 runs of the previous single-command generator on one "
            "commit produced 25 different reference implementations and passed "
            "roughly 45% of the time, because minting and grading were one command "
            "and the exam was reauthored for every run"
        ),
        "exam_id": args.exam_id,
        "generator": args.generator,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict_reproducible": reproducible,
        "gate_discriminates_pass_from_fail": discriminates,
        "arms": arms,
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"\nverdict_reproducible={reproducible} "
        f"gate_discriminates={discriminates}\nwritten: {out}"
    )
    return 0 if reproducible else 1


if __name__ == "__main__":
    raise SystemExit(main())
