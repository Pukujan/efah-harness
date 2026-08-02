"""Authority limits for the owner control surface.

Contract v1.1 §11.7 (AMENDMENT-001), §19.2, §17.2.

This module is the reason the surface is not a second orchestrator. It is a
deterministic classifier: no model participates in deciding whether a command is
permitted. That matters because a model-mediated gate would let a persuasive
instruction talk its way past the contract, which is exactly the
``free_form_llm_orchestrator`` non-goal.

GATE-D1-10 negative controls:

* **A6** an instruction that would expand scope is rejected, not executed
* **A7** the surface cannot bypass a gate or self-approve
* **A8** the surface cannot reach protected assets
"""

from __future__ import annotations

import re

from governance.protected import ALL_PROTECTED_MARKERS
from governance.states import DriftFinding

from .domain import MUTATING_VERBS, CommandOutcome, OwnerCommand, OwnerVerb, RejectionReason

#: Sealed-side and protected-instance names, from the single canonical
#: declaration. Reaching for any of these through the surface is
#: PROTECTED_ASSET_ACCESS regardless of intent (§17.2, §11.2, GATE-D1-08).
#: Imported rather than restated so a text scan finds one authorised definition
#: instead of a scatter of indistinguishable string constants.
PROTECTED_TERMS = (*(m.lower() for m in ALL_PROTECTED_MARKERS), ":6364")

#: Attempts to make a gate pass by decree rather than by evidence (§21.2).
GATE_BYPASS_PATTERNS = (
    r"\b(skip|bypass|disable|ignore|turn off|override|force[- ]?merge)\b.{0,40}\b(gate|check|test|holdout|mutation|oracle|ci)\b",
    r"\b(mark|set|declare)\b.{0,30}\b(passed|green|verified_complete|complete)\b",
    r"\bmerge\b.{0,30}\b(without|regardless|anyway|despite)\b",
    r"\bself[- ]?approve\b",
    r"\bauto[- ]?merge\b.{0,30}\b(now|immediately|without)\b",
    r"\bwithout\b.{0,20}\b(running|passing)\b.{0,20}\b(test|gate|check)\b",
)

#: Work the contract does not authorise. §19.2 UNAPPROVED_SCOPE_EXPANSION.
#: Deliberately conservative: it catches new task families, new runtimes the
#: contract excludes, and requirement weakening.
SCOPE_EXPANSION_PATTERNS = (
    r"\b(add|introduce|switch to|migrate to|use)\b.{0,30}\btemporal\b",
    r"\b(add|introduce|switch to|use)\b.{0,40}\b(claude (agent )?sdk|anthropic sdk)\b",
    r"\b(build|write|create)\b.{0,40}\b(our own|custom|new)\b.{0,30}\b(workflow engine|graph database|vector index|provider router|eval runner)\b",
    r"\b(drop|remove|relax|weaken|lower)\b.{0,40}\b(requirement|assertion|gate|acceptance)\b",
    r"\b(support|add)\b.{0,30}\b(new|another)\b.{0,20}\b(language|task family|domain)\b",
    r"\bexpand\b.{0,30}\b(scope|threat model|security)\b",
    r"\bamend\b.{0,20}\bcontract\b",
    r"\bchange\b.{0,25}\bcontract\b",
)

_GATE_BYPASS = [re.compile(p, re.I) for p in GATE_BYPASS_PATTERNS]
_SCOPE = [re.compile(p, re.I) for p in SCOPE_EXPANSION_PATTERNS]


def _reject(
    command: OwnerCommand, reason: RejectionReason, message: str, drift: DriftFinding | None = None
) -> CommandOutcome:
    return CommandOutcome(
        accepted=False,
        verb=command.verb,
        message=message,
        rejection_reason=reason,
        drift_finding=drift,
        command_hash=command.command_hash,
        entered_gate_path=True,
    )


def classify(command: OwnerCommand, *, known_targets: set[str] | None = None) -> CommandOutcome | None:
    """Return a rejection, or ``None`` if the command may proceed.

    Order matters: protected-asset access is checked first so that a request
    which is *both* a scope expansion and a sealed-side reach is reported as the
    more serious of the two.
    """
    haystack = f"{command.text} {command.target_id or ''}".lower()

    # A8 — protected assets. Checked for every verb, including OBSERVE: reading
    # holdout content is exactly the access the isolation gate forbids.
    for term in PROTECTED_TERMS:
        if term in haystack:
            return _reject(
                command,
                RejectionReason.PROTECTED_ASSET_ACCESS,
                f"Refused: {term!r} is sealed-side. The builder submits candidates and "
                "receives verdict shapes; it never reads protected content (contract §17.2).",
                DriftFinding.PROTECTED_ASSET_ACCESS,
            )

    # Contract binding. A command issued against a different contract version is
    # stale and must not be applied silently (§19.2 STALE_CONTRACT_VERSION).
    from governance.envelope import CONTRACT_VERSION

    if command.contract_version != CONTRACT_VERSION:
        return _reject(
            command,
            RejectionReason.STALE_CONTRACT_VERSION,
            f"Refused: command is bound to contract {command.contract_version}, "
            f"the governing version is {CONTRACT_VERSION}.",
            DriftFinding.STALE_CONTRACT_VERSION,
        )

    if command.verb not in MUTATING_VERBS and command.verb is not OwnerVerb.OBSERVE:
        return _reject(
            command, RejectionReason.NOT_A_PERMITTED_VERB, f"Refused: {command.verb} is not a surface verb."
        )

    # A7 — gate bypass and self-approval.
    for pattern in _GATE_BYPASS:
        if pattern.search(haystack):
            return _reject(
                command,
                RejectionReason.GATE_BYPASS_ATTEMPTED,
                "Refused: the surface cannot bypass a gate or self-approve. The gate is "
                "still required. CI performs the merge and the implementing agent does "
                "not self-certify (contract §21.2).",
            )

    # A6 — scope expansion. Only meaningful for instructions; resuming a work
    # unit cannot expand scope by itself.
    if command.verb is OwnerVerb.INSTRUCT:
        for pattern in _SCOPE:
            if pattern.search(haystack):
                return _reject(
                    command,
                    RejectionReason.UNAPPROVED_SCOPE_EXPANSION,
                    "Refused: UNAPPROVED_SCOPE_EXPANSION. The surface holds no authority "
                    "the contract does not already grant. A material change needs the §1.3 "
                    "amendment process — exact clause, impact analysis, owner approval, new "
                    "version, TerminusDB commit, recompiled gates, revalidation.",
                    DriftFinding.UNAPPROVED_SCOPE_EXPANSION,
                )

    if (
        command.verb in {OwnerVerb.RESUME, OwnerVerb.RETRY, OwnerVerb.CANCEL}
        and known_targets is not None
        and (command.target_id or "") not in known_targets
    ):
        return _reject(
            command,
            RejectionReason.UNKNOWN_TARGET,
            f"Refused: no work unit {command.target_id!r} in the authoritative graph.",
        )

    return None
