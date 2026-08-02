"""LangGraph-backed conversational graph for the owner control surface.

Contract v1.1 §11.7 requires the surface to be "a LangGraph-backed
conversational endpoint on the existing FastAPI application". This module is
that graph.

Shape::

    parse → classify → (reject) ─────────────┐
                     └→ apply → record → respond

``classify`` is deterministic (:mod:`owner_surface.policy`) and runs *before*
anything is applied. No model participates in deciding whether a command is
permitted — a model-mediated authority check would be the
``free_form_llm_orchestrator`` non-goal wearing a different hat.

The natural-language step is confined to ``parse`` and is optional: an explicit
verb bypasses it entirely, which is what lets the surface work with every
Anthropic credential removed (GATE-D1-10 A1/A2). Where a model *is* used it
routes through the **production** gateway, because the surface produces
candidate work, not gate-bearing evidence (DEC-002; GATE-D1-10 A10).
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from governance.envelope import utc_now
from governance.states import TaskState

from .domain import (
    MUTATING_VERBS,
    CommandOutcome,
    OwnerCommand,
    OwnerVerb,
    RejectionReason,
)
from .gateway import ControlPlaneGateway
from .policy import classify

#: The surface's gateway class. DEC-002: candidate work, never gate evidence.
GATEWAY_CLASS = "production"


class SurfaceState(TypedDict, total=False):
    raw_text: str
    explicit_verb: str | None
    target_id: str | None
    contract_version: str
    origin: str
    command: OwnerCommand
    outcome: CommandOutcome
    view: dict[str, Any]


_VERB_PATTERNS: list[tuple[OwnerVerb, re.Pattern[str]]] = [
    (OwnerVerb.RESUME, re.compile(r"\b(resume|continue|unpause|restart)\b", re.I)),
    (OwnerVerb.RETRY, re.compile(r"\b(retry|try again|rerun|re-run)\b", re.I)),
    (OwnerVerb.CANCEL, re.compile(r"\b(cancel|stop|abort|kill)\b", re.I)),
    (OwnerVerb.ANSWER_BLOCKER, re.compile(r"\b(answer|choose|option|decide|reply)\b", re.I)),
    (OwnerVerb.OBSERVE, re.compile(r"\b(status|state|show|what|where|how|progress|report)\b", re.I)),
]

_TARGET = re.compile(r"\b((?:WU|T|TASK|GATE)[- ]?[A-Z0-9-]+)\b", re.I)


def parse_command(state: SurfaceState) -> SurfaceState:
    """Turn owner input into a typed :class:`OwnerCommand`.

    Deterministic and dependency-free. An explicit verb from the UI wins; free
    text falls back to keyword matching, and anything unrecognised becomes
    ``INSTRUCT`` so it goes through the full scope check rather than being
    quietly treated as a read.
    """
    text = (state.get("raw_text") or "").strip()
    explicit = state.get("explicit_verb")

    if explicit:
        try:
            verb = OwnerVerb(explicit)
        except ValueError:
            verb = OwnerVerb.INSTRUCT
    else:
        verb = OwnerVerb.INSTRUCT
        for candidate, pattern in _VERB_PATTERNS:
            if pattern.search(text):
                verb = candidate
                break

    target = state.get("target_id")
    if not target:
        found = _TARGET.search(text)
        target = found.group(1).upper().replace(" ", "-") if found else None

    return {
        **state,
        "command": OwnerCommand(
            verb=verb,
            text=text,
            target_id=target,
            contract_version=state.get("contract_version", "1.1"),
        ),
    }


def build_graph(gateway: ControlPlaneGateway):
    """Compile the surface graph against a control-plane gateway."""

    async def classify_node(state: SurfaceState) -> SurfaceState:
        command: OwnerCommand = state["command"]
        known = await gateway.work_unit_ids() if command.verb in MUTATING_VERBS else None
        rejection = classify(command, known_targets=known)
        if rejection is not None:
            return {**state, "outcome": rejection}
        return state

    async def apply_node(state: SurfaceState) -> SurfaceState:
        """Apply a permitted command. Every path enters the normal gate path."""
        command: OwnerCommand = state["command"]

        if command.verb is OwnerVerb.OBSERVE:
            view = await gateway.project_view()
            return {
                **state,
                "view": view.model_dump(mode="json"),
                "outcome": CommandOutcome(
                    accepted=True,
                    verb=command.verb,
                    message=_summarise(view),
                    command_hash=command.command_hash,
                    entered_gate_path=False,
                ),
            }

        if command.verb is OwnerVerb.ANSWER_BLOCKER:
            blockers = await gateway.open_blockers()
            if not blockers:
                return {
                    **state,
                    "outcome": CommandOutcome(
                        accepted=False,
                        verb=command.verb,
                        message="No open typed blocker to answer.",
                        rejection_reason=RejectionReason.UNKNOWN_TARGET,
                        command_hash=command.command_hash,
                    ),
                }
            target = next((b for b in blockers if b.blocker_id == command.target_id), blockers[0])
            answered = target.model_copy(update={"answer": command.text, "answered_at": utc_now()})
            await gateway.upsert_blocker(answered)  # type: ignore[attr-defined]
            return {
                **state,
                "outcome": CommandOutcome(
                    accepted=True,
                    verb=command.verb,
                    message=(
                        f"Recorded as a Decision bound to contract v1.1 and answered "
                        f"{target.blocker_id} ({target.interrupt_type}). The task leaves "
                        f"BLOCKED_OWNER_DECISION and re-enters the gate path."
                    ),
                    command_hash=command.command_hash,
                    entered_gate_path=True,
                ),
            }

        if command.verb in {OwnerVerb.RESUME, OwnerVerb.RETRY, OwnerVerb.CANCEL}:
            new_state = {
                OwnerVerb.RESUME: TaskState.READY,
                OwnerVerb.RETRY: TaskState.REWORK_REQUIRED,
                OwnerVerb.CANCEL: TaskState.CANCELED,
            }[command.verb]
            return {
                **state,
                "outcome": CommandOutcome(
                    accepted=True,
                    verb=command.verb,
                    message=(
                        f"{command.target_id} → {new_state.value}. This is a request: it "
                        f"enters the same validation, drift and gate path as any other "
                        f"input, and does not mark anything PASSED."
                    ),
                    command_hash=command.command_hash,
                    entered_gate_path=True,
                ),
            }

        # INSTRUCT — survived the scope check, so it becomes a contract-bounded
        # request. It cannot self-approve; it queues for the normal path.
        return {
            **state,
            "outcome": CommandOutcome(
                accepted=True,
                verb=command.verb,
                message=(
                    "Queued as a contract-bounded instruction. It enters the normal "
                    "validation, drift and gate path; it grants no authority the "
                    "contract does not already grant."
                ),
                command_hash=command.command_hash,
                entered_gate_path=True,
            ),
        }

    async def record_node(state: SurfaceState) -> SurfaceState:
        """Bind the command to a durable, attributable record (§18)."""
        command: OwnerCommand = state["command"]
        outcome: CommandOutcome = state["outcome"]
        record_id, commit = await gateway.record_command(
            command,
            {
                "accepted": outcome.accepted,
                "rejection_reason": str(outcome.rejection_reason) if outcome.rejection_reason else None,
                "gateway_class": GATEWAY_CLASS,
                "origin": state.get("origin", ""),
            },
        )
        return {
            **state,
            "outcome": outcome.model_copy(update={"record_id": record_id, "terminus_commit": commit}),
        }

    def route_after_classify(state: SurfaceState) -> str:
        return "record" if state.get("outcome") is not None else "apply"

    graph = StateGraph(SurfaceState)
    graph.add_node("parse", parse_command)
    graph.add_node("classify", classify_node)
    graph.add_node("apply", apply_node)
    graph.add_node("record", record_node)

    graph.set_entry_point("parse")
    graph.add_edge("parse", "classify")
    graph.add_conditional_edges("classify", route_after_classify, {"apply": "apply", "record": "record"})
    graph.add_edge("apply", "record")
    graph.add_edge("record", END)
    return graph.compile()


def _summarise(view) -> str:
    parts = [
        f"{view.project_id} · {view.project_state} · contract {view.contract_version}",
        f"{view.tasks_passed}/{view.tasks_total} work units passed",
    ]
    if view.tasks_blocked:
        parts.append(f"{view.tasks_blocked} blocked")
    if view.open_blockers:
        parts.append(f"{len(view.open_blockers)} open owner blocker(s)")
    if view.terminus_database:
        parts.append(f"graph {view.terminus_database}@{view.terminus_branch or 'main'}")
    else:
        parts.append("graph not yet initialised")
    return " · ".join(parts)
