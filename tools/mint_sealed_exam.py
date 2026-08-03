#!/usr/bin/env python3
"""Mint one sealed exam, and record the pin the gate will grade against.

Minting is the expensive, non-deterministic half. It calls two frontier models
under the verifier identity, takes about three minutes, and is refused outright
unless every seeded mutant dies against the reference — measured on 2026-08-03,
that happens roughly 45% of the time, so expect to run this more than once.

None of that variance is a problem once minting is separated from grading. A
mint that fails costs three minutes and produces nothing; a mint that succeeds
freezes an exam under its own content hash, and every grade run afterwards is
deterministic, free, and answers the same question.

What this writes on the builder's side is a **pin**: ``evidence/sealed-exam-pin.json``
naming the exam by content hash. The hash is not a secret and holding it grants
nothing — the exam's content stays inside the 0700 store, and the builder cannot
read it. Keeping the pin here is what makes the binding between a verdict and an
exam auditable instead of implicit.

This tool never writes a pin for an exam the generator refused. A refused mint
has no identity to pin, and inventing one would put a name on an exercise that
failed its own validation.

Usage::

    PYTHONPATH=src python tools/mint_sealed_exam.py --target-count 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from verifier_identity.seam import (  # noqa: E402
    GenerationRequest,
    GenerationRunMode,
    GenerationSeam,
)

PIN = REPO_ROOT / "evidence" / "sealed-exam-pin.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-count", type=int, default=3)
    parser.add_argument("--candidate-commit", default=None)
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="mint attempts before giving up; each is a real run at real cost",
    )
    args = parser.parse_args()

    import subprocess

    commit = args.candidate_commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
    ).stdout.strip()

    seam = GenerationSeam(timeout_seconds=1200.0)
    for attempt in range(1, args.attempts + 1):
        request = GenerationRequest(
            generation_request_id=f"mint-{commit[:12]}-{int(time.time())}",
            candidate_commit=commit,
            contract_version="1.1",
            mode=GenerationRunMode.MINT,
            target_count=args.target_count,
        )
        outcome = seam.generate(request)
        receipt = outcome.receipt
        if receipt is None:
            print(f"attempt {attempt}: no receipt — {'; '.join(outcome.rejected_because)}")
            continue
        print(
            f"attempt {attempt}: exit {receipt.exit_status} "
            f"kill_rate {receipt.kill_rate} "
            f"({receipt.killed_count}/{receipt.mutant_count}) "
            f"reason {receipt.failure_reason} exam {receipt.exam_id}"
        )
        if not receipt.mint_accepted:
            continue

        PIN.parent.mkdir(parents=True, exist_ok=True)
        PIN.write_text(
            json.dumps(
                {
                    "exam_id": receipt.exam_id,
                    "minted_at": receipt.generated_at,
                    "minted_by_request": receipt.generation_request_id,
                    "candidate_commit": commit,
                    "holdout_count": receipt.holdout_count,
                    "mutant_count": receipt.mutant_count,
                    "kill_rate": receipt.kill_rate,
                    "generator_version": receipt.generator_version,
                    "oracle_version": receipt.oracle_version,
                    "note": (
                        "A content hash, not content. The exam itself is inside the "
                        "verifier identity's 0700 store and the builder cannot read "
                        "it; this file only says which exam a verdict is bound to."
                    ),
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\npinned: {receipt.exam_id}\nwritten: {PIN}")
        return 0

    print(f"\nno exam minted in {args.attempts} attempt(s); no pin written")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
