"""Contract approval and review use cases (contract Sections 1.3, 19.3, 19.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from api.context import RequestContext
from api.errors import ScopeExpansionRejected, StaleContractVersion
from api.ports import ControlPlaneWritePort
from api.state import DecisionRecord
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import ContractReviewOutcome
from observability.spans import Correlation, SpanKindName, efah_span


class ContractController:
    """``POST /contracts/{id}/approve`` and ``POST /contracts/{id}/review``."""

    def __init__(self, *, writer: ControlPlaneWritePort) -> None:
        self._writer = writer

    def approve(
        self,
        *,
        contract_id: str,
        approved_version: str,
        approver: str,
        rationale: str,
        context: RequestContext,
    ) -> DecisionRecord:
        """Record the owner's approval of an exact contract revision.

        Two refusals, both structural:

        * a different ``contract_id`` is not this harness's contract;
        * approving a version other than the governing one is
          ``STALE_CONTRACT_VERSION``, because an approval that does not name the
          revision it approves cannot be bound to anything (Section 18).
        """
        if contract_id != CONTRACT_ID:
            raise ScopeExpansionRejected(
                f"this harness governs {CONTRACT_ID}; it cannot approve {contract_id}"
            )
        if approved_version != CONTRACT_VERSION:
            raise StaleContractVersion(approved_version, CONTRACT_VERSION)

        decision = DecisionRecord(
            decision_id=f"APPROVE-{contract_id}-{approved_version}-{context.request_id[:8]}",
            title=f"Contract approval {contract_id}@{approved_version}",
            outcome="approved",
            decided_by=approver,
            decided_at=datetime.now(UTC).isoformat(),
            contract_version=approved_version,
            rationale=rationale,
        )
        with efah_span(
            "contract.approve",
            kind=SpanKindName.GATE,
            correlation=Correlation(project_id=contract_id, run_id=context.request_id),
        ):
            return self._writer.record_decision(decision)

    def review(
        self,
        *,
        contract_id: str,
        project_id: str,
        outcome: ContractReviewOutcome,
        reviewer: str,
        notes: str,
        context: RequestContext,
    ) -> DecisionRecord:
        """Section 19.4. Only ``CONTRACT_REAFFIRMED`` advances automatically."""
        if contract_id != CONTRACT_ID:
            raise ScopeExpansionRejected(
                f"this harness governs {CONTRACT_ID}; it cannot review {contract_id}"
            )
        with efah_span(
            "contract.review",
            kind=SpanKindName.GATE,
            correlation=Correlation(project_id=project_id, run_id=context.request_id),
            attributes={"outcome": str(outcome)},
        ):
            return self._writer.record_contract_review(
                project_id=project_id,
                outcome=str(outcome),
                reviewer=reviewer,
                notes=notes,
            )

    @staticmethod
    def advances_automatically(outcome: ContractReviewOutcome) -> bool:
        return outcome is ContractReviewOutcome.CONTRACT_REAFFIRMED
