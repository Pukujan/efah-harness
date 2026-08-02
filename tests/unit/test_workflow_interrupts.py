"""Contract Section 10.7 -- interrupts are a closed set, mechanically."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from governance.states import OwnerInterrupt
from workflows.interrupts import (
    PROHIBITED_INTERRUPT_REASONS,
    IllegalInterrupt,
    OwnerInterruptRequest,
    build_request,
    coerce_reason,
)

SRC = Path(__file__).resolve().parents[2] / "src"
INTERRUPT_MODULE = SRC / "workflows" / "interrupts.py"


def test_the_seven_types_are_exactly_the_contract_list():
    assert {str(x) for x in OwnerInterrupt} == {
        "OWNER_SCOPE_DECISION",
        "OWNER_PRIORITY_DECISION",
        "OWNER_RISK_ACCEPTANCE",
        "MISSING_REQUIRED_CREDENTIAL",
        "IRREVERSIBLE_EXTERNAL_ACTION",
        "CONTRACT_AMENDMENT_REQUIRED",
        "IRRESOLVABLE_EVIDENCE_CONFLICT",
    }


@pytest.mark.parametrize("reason", list(OwnerInterrupt))
def test_every_permitted_reason_is_accepted(reason: OwnerInterrupt):
    assert coerce_reason(reason) is reason
    assert coerce_reason(str(reason)) is reason


@pytest.mark.parametrize("reason", sorted(PROHIBITED_INTERRUPT_REASONS))
def test_autonomy_policy_prohibited_reasons_raise(reason: str):
    """``must_not_interrupt_for`` -- retry, CI repair, PR creation, auto-merge."""
    with pytest.raises(IllegalInterrupt) as excinfo:
        coerce_reason(reason)
    assert "must_not_interrupt_for" in str(excinfo.value)


@pytest.mark.parametrize(
    "reason",
    ["NEED_MORE_INFO", "OWNER_REVIEW", "", None, 7, OwnerInterrupt],
)
def test_an_invented_reason_raises_rather_than_interrupting(reason):
    with pytest.raises(IllegalInterrupt):
        coerce_reason(reason)


def test_an_interrupt_must_be_answerable():
    """Section 20.2: options, consequences, and what it blocks, or it is not a question."""
    with pytest.raises(ValueError):
        OwnerInterruptRequest(
            reason=OwnerInterrupt.OWNER_SCOPE_DECISION,
            what_it_blocks="WU-0001",
            options=["a", "b"],
            consequence_of_each_option=["only one consequence"],
        )
    with pytest.raises(ValueError):
        # A single "option" is a demand, not a decision.
        OwnerInterruptRequest(
            reason=OwnerInterrupt.OWNER_SCOPE_DECISION,
            what_it_blocks="WU-0001",
            options=["a"],
            consequence_of_each_option=["c"],
        )

    ok = build_request(
        OwnerInterrupt.OWNER_RISK_ACCEPTANCE,
        what_it_blocks="WU-0007",
        options=["accept", "reject"],
        consequence_of_each_option=["ship with the risk", "rework the work unit"],
        evidence=["sha256:abc"],
    )
    assert ok.reason is OwnerInterrupt.OWNER_RISK_ACCEPTANCE


def _calls_langgraph_interrupt(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "langgraph.types":
            if any(alias.name == "interrupt" for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "interrupt":
            return True
    return False


def test_only_the_interrupts_module_may_call_langgraph_interrupt():
    """A second call site is a second policy. Section 10.7 permits one."""
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path != INTERRUPT_MODULE and _calls_langgraph_interrupt(path)
    ]
    assert offenders == [], f"interrupt raised outside the Section 10.7 gate: {offenders}"
