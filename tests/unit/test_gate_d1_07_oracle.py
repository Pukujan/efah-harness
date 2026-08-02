"""The GATE-D1-07 A4 oracle must distinguish naming a vendor from needing one.

An earlier version flagged the step named "with Anthropic credentials removed" —
a step that proves the property rather than violating it. A gate that fires on
its own evidence is not measuring what it claims to measure.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gate_d1_07", ROOT / "tools" / "gate_d1_07.py")
gate = importlib.util.module_from_spec(spec)
sys.modules["gate_d1_07"] = gate
spec.loader.exec_module(gate)


def run_a4(tmp_path, workflow: str):
    ci = tmp_path / ".github" / "workflows"
    ci.mkdir(parents=True)
    (ci / "w.yml").write_text(workflow)
    original, gate.CI = gate.CI, ci
    try:
        return gate.a4_ci_has_no_claude_step()
    finally:
        gate.CI = original


def test_credential_removal_step_is_not_a_violation(tmp_path):
    ok, findings = run_a4(tmp_path, """
jobs:
  g:
    steps:
      - name: Run with Anthropic credentials removed
        env:
          ANTHROPIC_API_KEY: ""
        run: python tools/gate_d1_10.py
""")
    assert ok, findings


def test_action_that_uses_anthropic_is_a_violation(tmp_path):
    ok, findings = run_a4(tmp_path, """
jobs:
  g:
    steps:
      - uses: anthropics/claude-code-action@v1
""")
    assert not ok and "uses" in findings[0]


def test_step_invoking_claude_is_a_violation(tmp_path):
    ok, findings = run_a4(tmp_path, """
jobs:
  g:
    steps:
      - run: claude --print "do the thing"
""")
    assert not ok


def test_non_empty_anthropic_credential_is_a_violation(tmp_path):
    ok, findings = run_a4(tmp_path, """
jobs:
  g:
    steps:
      - env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: pytest
""")
    assert not ok and "bound to" in findings[0]


def test_real_workflows_pass_a4():
    """The repository's own CI must satisfy the assertion it enforces."""
    ok, findings = gate.a4_ci_has_no_claude_step()
    assert ok, findings
