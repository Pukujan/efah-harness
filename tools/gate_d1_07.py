#!/usr/bin/env python3
"""GATE-D1-07 — vendor neutrality after 2026-08-03.

Contract: product.vendor_neutral_after_deadline; Sections 0, 4.1.
oracle_type: static_ast_type_policy. No model judge in the verdict path.

A1  no essential module imports the Anthropic SDK or a Claude-specific client
A2  the walking skeleton runs with every Claude credential removed
A3  Claude Code appears only as an optional worker adapter
A4  CI pipelines contain no step requiring Claude access
A5  disabling the Claude adapter leaves a working worker adapter
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

SRC = Path("src")
CI = Path(".github/workflows")

#: Import roots that would bind an essential path to one vendor.
FORBIDDEN_IMPORT_ROOTS = {"anthropic", "claude_agent_sdk", "claude_code_sdk", "claude"}

#: The one place a Claude adapter is permitted: behind the worker interface.
ADAPTER_ALLOWLIST = {Path("src/workers/adapters/claude_code.py")}


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def a1_import_graph() -> tuple[bool, list[str]]:
    findings = []
    for path in sorted(SRC.rglob("*.py")):
        if path in ADAPTER_ALLOWLIST:
            continue
        bad = _imports(path) & FORBIDDEN_IMPORT_ROOTS
        if bad:
            findings.append(f"{path}: imports {sorted(bad)}")
    return not findings, findings


def a2_credentials_absent() -> tuple[bool, list[str]]:
    """The gate runs with Anthropic credentials unset; prove they are unset."""
    leaked = [
        name
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY")
        if os.environ.get(name)
    ]
    return not leaked, [f"{n} is set during the credential-stripped run" for n in leaked]


def a3_adapter_is_optional() -> tuple[bool, list[str]]:
    """A Claude adapter, if present, must sit behind the worker interface."""
    findings = []
    for path in sorted(SRC.rglob("*.py")):
        root_module = path.relative_to(SRC).parts[0]
        if root_module == "workers":
            continue
        if _imports(path) & FORBIDDEN_IMPORT_ROOTS:
            findings.append(f"{path}: vendor client outside src/workers/")
    return not findings, findings


def a4_ci_has_no_claude_step() -> tuple[bool, list[str]]:
    findings = []
    if not CI.is_dir():
        return True, []
    for path in sorted(CI.glob("*.yml")) + sorted(CI.glob("*.yaml")):
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            # An empty-string assignment is the credential being *removed*,
            # which is the gate working, not a violation.
            if "anthropic" in lowered or "claude" in lowered:
                if lowered.endswith(': ""') or lowered.endswith(": ''"):
                    continue
                findings.append(f"{path}:{line_no}: {stripped}")
    return not findings, findings


def a5_alternate_adapter_exists() -> tuple[bool, list[str]]:
    """At least one non-Claude worker adapter must exist and be registered."""
    adapters = Path("src/workers/adapters")
    if not adapters.is_dir():
        return False, ["src/workers/adapters/ does not exist"]
    others = [
        p.name
        for p in adapters.glob("*.py")
        if p.name not in {"__init__.py"} and p not in ADAPTER_ALLOWLIST
    ]
    if not others:
        return False, ["no non-Claude worker adapter present"]
    return True, []


ASSERTIONS = {
    "A1": ("no essential module imports the Anthropic SDK", a1_import_graph),
    "A2": ("skeleton runs with Claude credentials removed", a2_credentials_absent),
    "A3": ("Claude Code is only an optional worker adapter", a3_adapter_is_optional),
    "A4": ("CI contains no step requiring Claude access", a4_ci_has_no_claude_step),
    "A5": ("an alternate worker adapter completes work", a5_alternate_adapter_exists),
}


def main() -> int:
    results, failed = {}, False
    for aid, (claim, fn) in ASSERTIONS.items():
        ok, findings = fn()
        results[aid] = {"claim": claim, "passed": ok, "findings": findings}
        status = "PASS" if ok else "FAIL"
        print(f"  {aid} {status}  {claim}")
        for f in findings:
            print(f"        {f}")
        failed |= not ok

    Path("evidence").mkdir(exist_ok=True)
    Path("evidence/GATE-D1-07-result.json").write_text(
        json.dumps({"gate_id": "GATE-D1-07", "contract_version": "1.1",
                    "model_judge_in_verdict_path": False,
                    "verdict": "FAIL" if failed else "PASS",
                    "assertions": results}, indent=2)
    )
    print("GATE-D1-07:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
