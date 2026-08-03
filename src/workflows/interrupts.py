"""Owner interrupts -- Contract Section 10.7, mechanically closed.

    LangGraph interrupts MAY occur only for typed owner blockers [...] Routine
    implementation, retry, fallback, test repair, PR creation, or green
    auto-merge MUST NOT create an owner interrupt.

``autonomy-policy.yaml`` restates the same list twice, once as
``human_interrupts_only`` and once as ``must_not_interrupt_for``. A comment
saying "only interrupt for owner blockers" is worth nothing: politeness drift is
the failure mode, and it looks exactly like diligence. So the *only* way to
raise a LangGraph interrupt in this codebase is :func:`owner_interrupt`, which
takes an :class:`~governance.states.OwnerInterrupt` member. Passing a string --
"need permission to continue", "should I retry?" -- raises
:class:`IllegalInterrupt` before LangGraph ever sees it.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt as _langgraph_interrupt
from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import utc_now
from governance.states import OwnerInterrupt

#: ``autonomy-policy.yaml -> must_not_interrupt_for``. Named so that an attempt
#: to stop for one of them produces a diagnosis, not just a rejection.
PROHIBITED_INTERRUPT_REASONS: frozenset[str] = frozenset(
    {
        "ordinary_test_failure",
        "integration_failure",
        "ci_failure_repair",
        "retry_or_fallback_selection",
        "pr_creation",
        "green_auto_merge",
        "implementation_choices_within_contract",
        "refactoring_within_allowed_paths",
        "permission_to_continue",
    }
)


class IllegalInterrupt(RuntimeError):
    """An interrupt was attempted for a reason Section 10.7 does not allow."""

    def __init__(self, reason: Any) -> None:
        self.reason = reason
        hint = ""
        if isinstance(reason, str) and reason in PROHIBITED_INTERRUPT_REASONS:
            hint = (
                f" '{reason}' is named in autonomy-policy.yaml -> must_not_interrupt_for;"
                " continue autonomously and record the outcome."
            )
        super().__init__(
            f"contract Section 10.7 permits interrupts only for {sorted(OwnerInterrupt)}; "
            f"got {reason!r}.{hint}"
        )


class OwnerInterruptRequest(BaseModel):
    """The payload an owner sees. Section 20.2 requires it to be answerable.

    An interrupt that does not say what it blocks, what the options are, and
    what each option costs is not a question -- it is an idle loop with a
    prompt attached.
    """

    model_config = ConfigDict(extra="forbid")

    reason: OwnerInterrupt
    what_it_blocks: str
    options: list[str] = Field(min_length=2, max_length=4)
    consequence_of_each_option: list[str]
    evidence: list[str] = Field(default_factory=list)
    recommendation: str = ""
    confidence: str = ""
    work_unit_id: str = ""
    graph_node: str = ""
    raised_at: str = Field(default_factory=utc_now)

    def model_post_init(self, _context: Any) -> None:
        if len(self.consequence_of_each_option) != len(self.options):
            raise ValueError("each option must carry exactly one stated consequence (Section 20.2)")


def coerce_reason(reason: Any) -> OwnerInterrupt:
    """Return the typed reason, or raise :class:`IllegalInterrupt`.

    Accepts an ``OwnerInterrupt`` or the exact string of one. Anything else --
    including a plausible-sounding new reason -- is drift.
    """
    if isinstance(reason, OwnerInterrupt):
        return reason
    if isinstance(reason, str):
        try:
            return OwnerInterrupt(reason)
        except ValueError as exc:
            raise IllegalInterrupt(reason) from exc
    raise IllegalInterrupt(reason)


def build_request(
    reason: Any,
    *,
    what_it_blocks: str,
    options: list[str],
    consequence_of_each_option: list[str],
    evidence: list[str] | None = None,
    recommendation: str = "",
    confidence: str = "",
    work_unit_id: str = "",
    graph_node: str = "",
) -> OwnerInterruptRequest:
    return OwnerInterruptRequest(
        reason=coerce_reason(reason),
        what_it_blocks=what_it_blocks,
        options=options,
        consequence_of_each_option=consequence_of_each_option,
        evidence=list(evidence or []),
        recommendation=recommendation,
        confidence=confidence,
        work_unit_id=work_unit_id,
        graph_node=graph_node,
    )


def owner_interrupt(request: OwnerInterruptRequest) -> Any:
    """Raise a LangGraph interrupt for a typed owner blocker.

    This is the only call site of ``langgraph.types.interrupt`` in the harness;
    ``tests/unit/test_workflow_interrupts.py`` enforces that by scanning
    ``src/``.
    """
    coerce_reason(request.reason)
    return _langgraph_interrupt(request.model_dump(mode="json"))
