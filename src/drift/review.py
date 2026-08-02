"""Contract re-review scheduling and outcomes.

Contract Section 19.3: the project pack MUST contain
``contract_review_interval_phases``; the default is 3 material phases if
omitted. A review MUST also run at each of the contract's event triggers.

Section 19.4: only ``CONTRACT_REAFFIRMED`` advances automatically. Every other
outcome routes to typed remediation. Review is conformance checking, "not an
invitation to add optional improvements" -- so a review that emits new
requirements is itself drift (GATE-D2-22 A4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from governance.envelope import CONTRACT_VERSION
from governance.states import ContractReviewOutcome, DriftFinding, ProjectState

#: Section 19.3 default when the pack omits the field.
DEFAULT_INTERVAL_MATERIAL_PHASES = 3

#: Section 19.4. Everything else halts automatic advance.
ADVANCING_OUTCOME = ContractReviewOutcome.CONTRACT_REAFFIRMED

#: Section 19.4 -> typed remediation route per non-advancing outcome.
REMEDIATION_ROUTE: dict[ContractReviewOutcome, str] = {
    ContractReviewOutcome.DRIFT_DETECTED: "scope_drift_remediation",
    ContractReviewOutcome.EVIDENCE_STALE: "revalidate_stale_evidence",
    ContractReviewOutcome.RISK_CHANGED: "owner_risk_acceptance",
    ContractReviewOutcome.CONTRACT_AMBIGUITY: "owner_clarification",
    ContractReviewOutcome.AMENDMENT_REQUIRED: "contract_amendment_process_section_1_3",
}

#: Non-advancing outcomes that end the run rather than looping.
TERMINAL_ROUTE: dict[ContractReviewOutcome, ProjectState] = {
    ContractReviewOutcome.CONTRACT_AMBIGUITY: ProjectState.BLOCKED_OWNER_DECISION,
    ContractReviewOutcome.AMENDMENT_REQUIRED: ProjectState.BLOCKED_OWNER_DECISION,
}


@dataclass(frozen=True)
class ReviewTrigger:
    trigger_id: str
    trigger_type: str  # "periodic" | "event"
    reason: str
    phases_since_last_review: int | None = None
    event: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "reason": self.reason,
            "phases_since_last_review": self.phases_since_last_review,
            "event": self.event,
            "contract_ref": "contract.md#19.3",
        }


@dataclass
class ContractReview:
    review_id: str
    trigger: ReviewTrigger
    outcome: ContractReviewOutcome
    contract_version: str = CONTRACT_VERSION
    findings: list[dict[str, Any]] = field(default_factory=list)
    requirements_before: list[str] = field(default_factory=list)
    requirements_after: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def advances_automatically(self) -> bool:
        return self.outcome is ADVANCING_OUTCOME

    @property
    def added_requirements(self) -> list[str]:
        return sorted(set(self.requirements_after) - set(self.requirements_before))

    @property
    def scope_expanded(self) -> bool:
        """GATE-D2-22 A4: a review must not add requirements."""
        return bool(self.added_requirements)

    def as_body(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "trigger": self.trigger.as_body(),
            "outcome": str(self.outcome),
            "contract_version": self.contract_version,
            "advances_automatically": self.advances_automatically,
            "remediation_route": REMEDIATION_ROUTE.get(self.outcome),
            "terminal_state": str(TERMINAL_ROUTE[self.outcome]) if self.outcome in TERMINAL_ROUTE else None,
            "findings": self.findings,
            "added_requirements": self.added_requirements,
            "scope_expanded": self.scope_expanded,
            "scope_expansion_finding": str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION) if self.scope_expanded else None,
            "evidence": self.evidence,
            "contract_ref": "contract.md#19.3,#19.4",
        }


class ContractReviewScheduler:
    """Decides when a conformance review is due. No model involved."""

    def __init__(self, interval_material_phases: int | None, event_triggers: Iterable[str]) -> None:
        self.interval_material_phases = (
            int(interval_material_phases)
            if interval_material_phases is not None
            else DEFAULT_INTERVAL_MATERIAL_PHASES
        )
        if self.interval_material_phases < 1:
            raise ValueError("contract_review_interval_phases must be at least 1 material phase")
        self.event_triggers = tuple(event_triggers)
        self._counter = 0

    @classmethod
    def from_pack(cls, pack: Any) -> "ContractReviewScheduler":
        project = pack.yaml("project.yaml")["project"]
        contract = pack.yaml("contract.yaml")["contract_review"]
        return cls(
            project.get("contract_review_interval_phases"),
            contract.get("event_triggers", []),
        )

    # -- scheduling --------------------------------------------------------

    def due_for_phases(self, phases_since_last_review: int) -> ReviewTrigger | None:
        if phases_since_last_review >= self.interval_material_phases:
            return ReviewTrigger(
                trigger_id="CRT-INTERVAL",
                trigger_type="periodic",
                reason=(
                    f"{phases_since_last_review} material phases completed; interval is "
                    f"{self.interval_material_phases}"
                ),
                phases_since_last_review=phases_since_last_review,
            )
        return None

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        if event in self.event_triggers:
            return ReviewTrigger(
                trigger_id=f"CRT-EV-{self.event_triggers.index(event) + 1:02d}",
                trigger_type="event",
                reason=f"event trigger {event}",
                event=event,
            )
        return None

    def observe_phase(self, material: bool = True) -> ReviewTrigger | None:
        """Advance the phase counter and return a trigger when the interval is hit."""
        if material:
            self._counter += 1
        trigger = self.due_for_phases(self._counter)
        if trigger is not None:
            self._counter = 0
        return trigger

    # -- outcomes ----------------------------------------------------------

    @staticmethod
    def advances_automatically(outcome: ContractReviewOutcome) -> bool:
        return outcome is ADVANCING_OUTCOME

    def review(
        self,
        *,
        review_id: str,
        trigger: ReviewTrigger,
        drift_findings: Sequence[dict[str, Any]],
        requirements_before: Sequence[str],
        requirements_after: Sequence[str],
        evidence: Sequence[str] = (),
        stale_evidence: bool = False,
        risk_changed: bool = False,
        ambiguity: bool = False,
        amendment_required: bool = False,
    ) -> ContractReview:
        """Derive the Section 19.4 outcome from observed conformance only."""
        if amendment_required:
            outcome = ContractReviewOutcome.AMENDMENT_REQUIRED
        elif ambiguity:
            outcome = ContractReviewOutcome.CONTRACT_AMBIGUITY
        elif risk_changed:
            outcome = ContractReviewOutcome.RISK_CHANGED
        elif stale_evidence:
            outcome = ContractReviewOutcome.EVIDENCE_STALE
        elif drift_findings:
            outcome = ContractReviewOutcome.DRIFT_DETECTED
        elif set(requirements_after) - set(requirements_before):
            # Section 19.4: review is conformance checking. A review that adds
            # requirements has drifted, whatever else it found.
            outcome = ContractReviewOutcome.DRIFT_DETECTED
        else:
            outcome = ContractReviewOutcome.CONTRACT_REAFFIRMED
        return ContractReview(
            review_id=review_id,
            trigger=trigger,
            outcome=outcome,
            findings=list(drift_findings),
            requirements_before=list(requirements_before),
            requirements_after=list(requirements_after),
            evidence=list(evidence),
        )
