#!/usr/bin/env python3
"""GATE-D1-10 — vendor-neutral owner control surface.

Contract v1.1 Section 11.7, added by AMENDMENT-001.
oracle_type: deterministic_execution_or_state. No model judge in the verdict path.

Runs the surface's own negative controls in-process and records the result as
evidence. The assertions live in tests/contract/test_owner_surface.py; this tool
is the gate wrapper that CI and the gate runner call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ASSERTIONS = {
    "A1": ("responds with Anthropic credentials unset", "test_a1_"),
    "A2": ("no Anthropic import in any surface code path", "test_a2_"),
    "A3": ("owner reads project and task state", "test_a3_"),
    "A4": ("owner answers an open typed blocker", "test_a4_"),
    "A5": ("owner can resume, retry, cancel", "test_a5_"),
    "A6": ("scope expansion rejected, not executed", "test_a6_"),
    "A7": ("cannot bypass a gate or self-approve", "test_a7_"),
    "A8": ("cannot reach protected assets", "test_a8_"),
    "A9": ("usable from a mobile viewport, self-contained", "test_a9_"),
    "A10": ("routes through the production gateway", "test_a10_"),
}

TESTS = "tests/contract/test_owner_surface.py"


def main() -> int:
    env = dict(os.environ)
    # A1/A2 are only meaningful if the credentials really are absent.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        env.pop(name, None)

    results, failed = {}, False
    for aid, (claim, prefix) in ASSERTIONS.items():
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", TESTS, "-k", prefix, "-q", "--no-header"],
            capture_output=True, text=True, env=env,
        )
        ok = proc.returncode == 0
        results[aid] = {"claim": claim, "passed": ok,
                        "detail": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""}
        print(f"  {aid} {'PASS' if ok else 'FAIL'}  {claim}")
        if not ok:
            print("        " + (proc.stdout or proc.stderr).strip()[-600:])
        failed |= not ok

    Path("evidence").mkdir(exist_ok=True)
    Path("evidence/GATE-D1-10-result.json").write_text(json.dumps(
        {"gate_id": "GATE-D1-10", "contract_version": "1.1",
         "model_judge_in_verdict_path": False,
         "verdict": "FAIL" if failed else "PASS", "assertions": results}, indent=2))
    print("GATE-D1-10:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
