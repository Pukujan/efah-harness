#!/usr/bin/env python3
"""Build the contract §27 final evidence package (GATE-D3-26).

    PYTHONPATH=src python tools/build_evidence_package.py
    PYTHONPATH=src python tools/build_evidence_package.py --run-tests

``--run-tests`` executes the visible suite and records it the way §18 requires a
test result to be recorded — command, environment, timestamp, exit status, raw
result artifact, and the commit it is bound to. Without it, the visible-tests
field is ``UNAVAILABLE``, because a test result not bound to a commit is not
evidence about that commit.

**Exit status is the package's completeness, not the project's success.** This
tool exits non-zero while §27 fields are unmeasured, and today four of them are.
It does not and must not exit zero to signal that the build is finished: §6.2
reserves that for ``VERIFIED_COMPLETE``, which the gate run decides, not this
tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evidence.package import build, render_text, write  # noqa: E402


def run_visible_tests(raw_out: Path) -> dict[str, Any]:
    """Run the suite and record it as §18 requires a test result to be recorded."""
    from evaluation.binding import resolve_head

    commit = resolve_head()
    command = [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"]
    started = time.time()
    proc = subprocess.run(  # noqa: S603 - fixed argv
        command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800
    )
    finished = time.time()

    raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)

    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    summary = tail[-1] if tail else ""
    return {
        "command": " ".join(command),
        "exit_status": proc.returncode,
        "result": "PASS" if proc.returncode == 0 else "FAIL",
        "summary": summary,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_seconds": round(finished - started, 1),
        "candidate_commit": commit,
        "raw_result_artifact": str(raw_out.relative_to(REPO_ROOT)),
        "raw_result_sha256": "sha256:" + hashlib.sha256(raw_out.read_bytes()).hexdigest(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            # Recorded because a suite that ran with different credentials
            # present is a different suite; the names only, never the values.
            "credential_names_present": sorted(
                k
                for k in os.environ
                if k.endswith(("_KEY", "_PASS", "_TOKEN")) and os.environ.get(k)
            ),
        },
    }


def build_artifact() -> dict[str, Any]:
    """Build the wheel and record what it was built from.

    The digest is only meaningful with the commit beside it, and only if that
    commit's tree was clean — a wheel built from a dirty tree identifies nothing.
    Both facts are written next to the artifact rather than assumed later.
    """
    from evaluation.binding import resolve_head

    dirty = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    commit = resolve_head()

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--wheel", "--outdir", "dist", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        return {"built": False, "error": proc.stderr[-500:]}

    manifest = {
        "producer": f"python -m build --wheel (python {sys.version.split()[0]})",
        "commit": commit,
        "working_tree_clean": dirty == "",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
    }
    (REPO_ROOT / "dist" / "BUILD-PROVENANCE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"built": True, **manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-tests", action="store_true", help="execute and bind the visible suite")
    parser.add_argument("--build-artifact", action="store_true", help="build the wheel and record its provenance")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "evidence")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.build_artifact:
        print("building the release artifact...", file=sys.stderr)
        result = build_artifact()
        if not result.get("built"):
            print(f"  build failed: {result.get('error')}", file=sys.stderr)
        else:
            clean = "clean" if result["working_tree_clean"] else "DIRTY"
            print(f"  built from {result['commit'][:12]} ({clean} tree)", file=sys.stderr)

    test_report = None
    if args.run_tests:
        print("running the visible suite (this takes a few minutes)...", file=sys.stderr)
        test_report = run_visible_tests(args.out_dir / "visible-tests-raw.txt")
        print(f"  {test_report['result']}: {test_report['summary']}", file=sys.stderr)

    package = build(test_report=test_report)
    json_path, text_path = write(package, args.out_dir)

    if not args.quiet:
        print(render_text(package))
    print(f"\nwritten: {json_path}\n         {text_path}", file=sys.stderr)

    if package.missing:
        print(
            f"\n{len(package.missing)} §27 field(s) unmeasured: {', '.join(package.missing)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
