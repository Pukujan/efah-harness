#!/usr/bin/env python3
"""Verify no visible gate assertion changed without an amendment.

Contract Section 14.3: visible behavioural assertions MUST be hashed before
convergence, and a change requires an explicit test amendment linked to the
contract. A builder that can silently weaken a gate to make it green has no
gates at all.

Exits non-zero on any drift. Mirrors the pack's own hash_assertions.py so the
check runs in CI without the pack's tooling on PATH.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

GATE_DIR = Path("project-pack/acceptance/visible")
MANIFEST = GATE_DIR / "ASSERTION_HASHES.txt"
LINE = re.compile(r"^sha256:([0-9a-f]{64})\s+(\S+)$")


def main() -> int:
    if not MANIFEST.is_file():
        print(f"FAIL: assertion manifest missing at {MANIFEST}", file=sys.stderr)
        print("A missing manifest is itself a finding, not a pass.", file=sys.stderr)
        return 1

    expected: dict[str, str] = {}
    for raw in MANIFEST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if m:
            expected[m.group(2)] = m.group(1)

    violations: list[str] = []
    for name, want in sorted(expected.items()):
        path = GATE_DIR / name
        if not path.is_file():
            violations.append(f"{name}: gate file deleted")
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            violations.append(f"{name}: assertion hash changed ({got[:12]} != {want[:12]})")

    present = {p.name for p in GATE_DIR.glob("GATE-*.yaml")}
    for name in sorted(present - set(expected)):
        violations.append(f"{name}: gate present but absent from the manifest")

    if violations:
        print("FAILED_SCOPE: unauthorized assertion change", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(f"{len(expected)} gates checked, 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
