"""Modes, and the deterministic table that turns one into a graph.

`BUILD_VS_INTEGRATE-001` selects a mature chat client rather than a hand-written
UI, and the client's **model dropdown** becomes the mode selector. That is the
whole trick: a chat client already has a control for "which thing am I talking
to", so modes arrive as configuration instead of as a UI to build.

The dispatch below is a **table**, not a classifier. No model decides which mode
it is in, for the same reason :mod:`owner_surface.policy` decides authority
without a model: a mode chooses which graph runs, which roles it may use and
which gates apply, and letting the thing being governed pick its own governance
is the ``free_form_llm_orchestrator`` non-goal wearing a different hat.

The owner picks the mode explicitly, or gets :data:`DEFAULT_MODE`. There is no
inference from message text — a message that *sounds* like research does not
become research mode, because "sounds like" is exactly the judgment that must not
sit in the control path.

Why a mode is not just a prompt
--------------------------------
Each mode names a **role**, and the role is what the model router resolves to an
alias through the pack. So selecting "research" does not merely change wording;
it routes to ``researcher``, which is a different vendor family from
``implementer`` by §12.2, and it lands on the gateway class DEC-002 assigns that
role. A mode that only changed the system prompt would leave every request on
the implementer's model and quietly break role separation while looking like it
worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: What the owner sees in the client's model list. Prefixed so a harness mode is
#: never mistaken for an upstream model — the client shows these beside real
#: model names, and ``efah-`` is the only thing keeping them apart.
MODEL_PREFIX = "efah-"


@dataclass(frozen=True)
class Mode:
    """One selectable way of working."""

    name: str
    role: str
    summary: str
    #: The LangGraph graph this mode runs, or ``None`` for a single-turn
    #: exchange that needs no graph. ``None`` is honest rather than lazy: a
    #: one-shot question does not become more rigorous by being routed through
    #: a build graph, and pretending otherwise would put a lease and a gate
    #: sequence around "what does this flag do".
    graph_id: str | None
    #: Prepended to the owner's message. Never overrides it — the owner's text
    #: is data, and the framing says so.
    system: str
    max_tokens: int = 2048

    @property
    def model_id(self) -> str:
        return f"{MODEL_PREFIX}{self.name}"


MODES: tuple[Mode, ...] = (
    Mode(
        name="auto",
        role="implementer",
        summary="One turn, answered directly. The default.",
        graph_id=None,
        system=(
            "You are the EFAH harness answering one question from the project owner. "
            "Answer concretely and say plainly when you do not know. Do not claim work "
            "you did not do."
        ),
    ),
    Mode(
        name="plan",
        role="planner",
        summary="Decompose into work units with dependencies. Does not execute.",
        graph_id="planning",
        system=(
            "You are planning work under contract EFAH-CONTRACT-001. Produce work units "
            "with explicit dependencies and success conditions. Do not implement anything; "
            "a plan that starts writing code is not a plan. State what you would need to "
            "know that you do not."
        ),
        max_tokens=4096,
    ),
    Mode(
        name="research",
        role="researcher",
        summary="Evidence-first. Every load-bearing claim carries a source.",
        graph_id="intake",
        system=(
            "You are researching under contract §7.3, which requires every load-bearing "
            "claim to record its source, the exact supporting location, and whether the "
            "source states the claim directly or the claim is inferred from it. Quote the "
            "source rather than paraphrasing, so the quote can be checked against it. "
            "Where you have no source, say INSUFFICIENT_EVIDENCE rather than answering "
            "from memory — an unsourced answer is the failure this mode exists to prevent."
        ),
        max_tokens=6144,
    ),
    Mode(
        name="review",
        role="adversarial_critic",
        summary="Tries to refute. Cross-family from the implementer by §12.2.",
        graph_id=None,
        system=(
            "You are the adversarial critic. Your job is to find what is wrong, not to "
            "agree. §12.4: a producing model must not be the sole reviewer of its own "
            "output, and you are the independent one. Be specific — name the failure "
            "case, not the feeling."
        ),
        max_tokens=4096,
    ),
    Mode(
        name="build",
        role="implementer",
        summary="Executes a work unit through the build graph, with a lease and gates.",
        graph_id="build",
        system=(
            "You are implementing one work unit under contract EFAH-CONTRACT-001. Produce "
            "a candidate; you do not certify it. §21.2 forbids the implementing agent "
            "self-certifying, so state what you changed and let the gates decide."
        ),
        max_tokens=8192,
    ),
)

BY_NAME: dict[str, Mode] = {m.name: m for m in MODES}
BY_MODEL_ID: dict[str, Mode] = {m.model_id: m for m in MODES}

#: What an unspecified request gets. ``auto`` and not ``build``: a request that
#: did not name a mode has not asked for a lease, a branch, or a gate sequence.
DEFAULT_MODE = BY_NAME["auto"]


class UnknownMode(ValueError):
    """The client asked for a model id that is not a declared mode."""


def resolve(model_id: str | None) -> Mode:
    """Map a client's ``model`` field to a mode. Deterministic, total.

    An unknown id **raises** rather than falling back to the default. A chat
    client that asks for ``gpt-4o`` is asking to talk to a model, not to the
    harness, and silently answering as ``auto`` would look like it worked — the
    exact confusion `BUILD_VS_INTEGRATE-001` names as the trap, where a client
    pointed at the gateway bypasses every gate and still returns text.
    """
    if not model_id:
        return DEFAULT_MODE
    mode = BY_MODEL_ID.get(model_id.strip())
    if mode is None:
        raise UnknownMode(
            f"{model_id!r} is not an EFAH mode. Available: "
            + ", ".join(m.model_id for m in MODES)
        )
    return mode


def model_list_payload() -> dict[str, Any]:
    """The OpenAI ``/v1/models`` shape, listing modes.

    Open WebUI can allowlist models by hand, so this endpoint is a convenience
    rather than a requirement. It is served anyway because a client that can
    discover the modes shows the owner what is available instead of making them
    remember.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": mode.model_id,
                "object": "model",
                "owned_by": "efah",
                # Not an OpenAI field. Clients that render a description show
                # the owner what the mode does; those that do not, ignore it.
                "description": mode.summary,
            }
            for mode in MODES
        ],
    }
