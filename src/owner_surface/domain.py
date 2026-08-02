"""Owner control surface — domain model and authority limits.

Contract EFAH-CONTRACT-001 **v1.1 §11.7**, added by AMENDMENT-001.

The surface exists to close the *control* half of
``product.vendor_neutral_after_deadline``. Execution is already vendor-neutral;
without this, the owner's only ways to drive the harness die with Claude Code
access on 2026-08-03.

**What this module is not.** The amendment is explicit: the surface

- is not a second orchestrator;
- holds no authority the API and contract do not already grant;
- cannot change scope, approve its own requests, bypass a gate, alter the
  contract, or reach protected assets.

Every command it accepts is a *request* that enters the same validation, drift,
and gate path as any other input. Those limits are expressed here as data and
enforced in :mod:`owner_surface.policy`, so they are testable rather than
aspirational — GATE-D1-10 A6, A7 and A8 are negative controls that must reject.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import CONTRACT_VERSION, content_hash, utc_now
from governance.states import DriftFinding, OwnerInterrupt, TaskState


class OwnerVerb(StrEnum):
    """The closed set of things the owner may do through the surface.

    §11.7 names exactly four capabilities: observe state, answer an open typed
    blocker, resume/retry/cancel a work unit, and issue a new contract-bounded
    instruction. Nothing else is in scope, and a verb that is not here is not a
    verb the surface has.
    """

    OBSERVE = "OBSERVE"
    ANSWER_BLOCKER = "ANSWER_BLOCKER"
    RESUME = "RESUME"
    RETRY = "RETRY"
    CANCEL = "CANCEL"
    INSTRUCT = "INSTRUCT"


#: Verbs that mutate. Each still enters the normal gate path; none self-approve.
MUTATING_VERBS = frozenset(
    {
        OwnerVerb.ANSWER_BLOCKER,
        OwnerVerb.RESUME,
        OwnerVerb.RETRY,
        OwnerVerb.CANCEL,
        OwnerVerb.INSTRUCT,
    }
)


class RejectionReason(StrEnum):
    """Why a command was refused. Every value maps to a contract failure state."""

    UNAPPROVED_SCOPE_EXPANSION = "UNAPPROVED_SCOPE_EXPANSION"
    GATE_BYPASS_ATTEMPTED = "GATE_BYPASS_ATTEMPTED"
    PROTECTED_ASSET_ACCESS = "PROTECTED_ASSET_ACCESS"
    CONTRACT_AMENDMENT_REQUIRED = "CONTRACT_AMENDMENT_REQUIRED"
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    NOT_A_PERMITTED_VERB = "NOT_A_PERMITTED_VERB"
    STALE_CONTRACT_VERSION = "STALE_CONTRACT_VERSION"


class OwnerCommand(BaseModel):
    """One instruction from the owner. A request, never an authorisation."""

    model_config = ConfigDict(extra="forbid")

    verb: OwnerVerb
    text: str = ""
    target_id: str | None = None
    contract_version: str = CONTRACT_VERSION
    received_at: str = Field(default_factory=utc_now)

    @property
    def command_hash(self) -> str:
        return content_hash(
            {"verb": str(self.verb), "text": self.text, "target_id": self.target_id}
        )


class CommandOutcome(BaseModel):
    """The result of submitting a command. Records refusals as first-class."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    verb: OwnerVerb
    message: str
    rejection_reason: RejectionReason | None = None
    drift_finding: DriftFinding | None = None
    #: Set when the command produced a durable record (a Decision, a task
    #: transition). Bound to a TerminusDB commit where the graph is reachable.
    record_id: str | None = None
    terminus_commit: str | None = None
    command_hash: str | None = None
    entered_gate_path: bool = False


class OpenBlocker(BaseModel):
    """A typed owner blocker awaiting an answer.

    §10.7 keeps this list closed: the surface is how the owner *answers* a
    blocker, and it does not create new interrupt types.
    """

    model_config = ConfigDict(extra="forbid")

    blocker_id: str
    interrupt_type: OwnerInterrupt
    task_id: str | None = None
    question: str
    options: list[str] = Field(default_factory=list)
    raised_at: str = Field(default_factory=utc_now)
    answered_at: str | None = None
    answer: str | None = None

    def option_keys(self) -> list[str]:
        """The selectable keys, parsed from the declared options.

        An option reads ``"A - official credentials… CONSEQUENCE: …"``. The key
        is the leading letter. Entries with no key — the RECOMMENDATION block —
        are guidance, not choices, and are deliberately not selectable.
        """
        keys: list[str] = []
        for option in self.options:
            head = str(option).strip()
            # Hyphen, en dash, em dash or space — options are written by hand
            # and the separator varies. Escaped so the ambiguous-character lint
            # does not have to guess what a bare dash is.
            if len(head) >= 2 and head[0].isalnum() and head[1] in "-\u2013\u2014 .":
                keys.append(head[0].upper())
        return keys

    def accepts(self, answer: str) -> bool:
        """Whether ``answer`` selects one of this blocker's declared options.

        A blocker that declares no options takes free text — some typed
        blockers genuinely want a value rather than a choice. One that *does*
        declare options must be answered with one of them: an
        ``OWNER_RISK_ACCEPTANCE`` question offering A/C/D was once closed by the
        word "Hello", which is how this method came to exist.
        """
        keys = self.option_keys()
        if not keys:
            return bool(answer.strip())
        return answer.strip().upper().rstrip(".").split()[0] in keys if answer.strip() else False


class WorkUnitView(BaseModel):
    """A read projection of a work unit. Read-only by construction (§5.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_unit_id: str
    task_id: str
    objective: str
    state: TaskState
    #: Blinded alias only. §11.2 — the real model identity lives in the
    #: protected store and never reaches a task-facing projection.
    assigned_alias: str | None = None
    lease_generation: int | None = None
    pending_gates: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now)


class InstructionExchange(BaseModel):
    """One instruction and what came back. A read projection, never truth.

    The surface is not a chat client and this is not a transcript: each exchange
    is an independent request with no thread and no carried context. It exists
    so the owner can see the *result* of an instruction next to the instruction,
    which is the minimum for the surface to be usable at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    instruction: str
    issued_at: str
    state: str | None = None
    #: Blinded alias only (§12.3). Never a vendor, family, or model id.
    assigned_alias: str | None = None
    result: str | None = None
    failure_class: str | None = None
    completed_at: str | None = None

    @property
    def pending(self) -> bool:
        return self.state is None


class ProjectView(BaseModel):
    """Top-level state the owner sees first on a phone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    project_state: str
    contract_id: str
    contract_version: str
    terminus_database: str | None = None
    terminus_branch: str | None = None
    terminus_commit: str | None = None
    tasks_total: int = 0
    tasks_passed: int = 0
    tasks_blocked: int = 0
    open_blockers: list[OpenBlocker] = Field(default_factory=list)
    work_units: list[WorkUnitView] = Field(default_factory=list)
    #: Recent instruction exchanges, newest last. Added after the owner opened
    #: the surface and found no way to see what came back from an instruction:
    #: the consumer wrote results into the ledger and nothing displayed them, so
    #: issuing work felt identical to issuing nothing.
    exchanges: list[InstructionExchange] = Field(default_factory=list)
    generated_at: str = Field(default_factory=utc_now)
