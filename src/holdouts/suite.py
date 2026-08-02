"""Hidden holdout lane — build-side half only.

Contract Sections 12.2, 17.1, 17.2. This module deliberately contains **no
holdout cases**. Sealed release holdouts are authored by the
``sealed_holdout_author`` role under the verifier service identity and live in
the sealed repository; ``repositories.yaml`` sets
``builder_may_read_generated_holdouts: false``. A holdout the builder wrote is
not a holdout, it is a test the builder already passed.

What the build side legitimately owns is the *lane*: a request for evaluation
bound to an exact candidate commit, submitted through the four-field interface,
and a typed blocker when the sealed side is unreachable.

Current state, honestly: no sealed holdout content exists yet (open owner
question Q1) and no verifier endpoint is configured. :meth:`HoldoutLane.run`
therefore returns ``UNVERIFIABLE`` with ``BLOCKED_EXTERNAL_ACCESS``. That is the
correct output. Manufacturing a local "hidden" suite to turn the lane green
would make the evaluation circular, which is the failure the sealed side exists
to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evaluation.binding import CandidateBinding, Lane, LaneRun
from evaluation.verifier_client import (
    ProtectedVerifierClient,
    VerifierOutcome,
    build_submission,
)
from governance.states import ProjectState, Verdict


@dataclass
class HoldoutLaneResult:
    lane_run: LaneRun
    outcome: VerifierOutcome | None
    blocked_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "lane": Lane.HIDDEN.value,
            "candidate_commit": self.lane_run.candidate_commit,
            "verdict": self.lane_run.verdict.value,
            "blocked_reason": self.blocked_reason,
            "holdout_content_present_on_build_side": False,
            "verifier_outcome": self.outcome.as_evidence() if self.outcome else None,
            "notes": self.notes,
        }


class HoldoutLane:
    """Submits a candidate commit for hidden evaluation. Reads nothing back but a shape."""

    def __init__(self, client: ProtectedVerifierClient | None = None) -> None:
        #: Unconfigured by default, and that is the correct build-side state.
        self._client = client or ProtectedVerifierClient()

    def run(
        self,
        binding: CandidateBinding,
        *,
        evaluation_request_id: str,
        required_contract_or_oracle_version: str | None = None,
        allowed_runtime_inputs: dict[str, str] | None = None,
    ) -> HoldoutLaneResult:
        submission = build_submission(
            artifact_or_commit_identifier=binding.commit_sha,
            evaluation_request_id=evaluation_request_id,
            required_contract_or_oracle_version=(
                required_contract_or_oracle_version or binding.contract_version
            ),
            allowed_runtime_inputs=allowed_runtime_inputs,
        )
        outcome = self._client.submit(submission)

        if outcome.state is ProjectState.BLOCKED_EXTERNAL_ACCESS:
            return HoldoutLaneResult(
                lane_run=LaneRun(
                    lane=Lane.HIDDEN,
                    candidate_commit=binding.commit_sha,
                    verdict=Verdict.UNVERIFIABLE,
                    detail="protected verifier unreachable from the build side",
                ),
                outcome=outcome,
                blocked_reason="; ".join(outcome.rejected_because or []),
                notes=[
                    (
                        "Sealed holdout content is not authored on the build side "
                        "(repositories.yaml builder_may_read_generated_holdouts: false)."
                    ),
                    "No local fallback is provided: a locally-evaluated holdout is circular.",
                ],
            )

        if not outcome.accepted:
            return HoldoutLaneResult(
                lane_run=LaneRun(
                    lane=Lane.HIDDEN,
                    candidate_commit=binding.commit_sha,
                    verdict=Verdict.FAIL,
                    detail="verifier response rejected by the build-side client",
                ),
                outcome=outcome,
                blocked_reason="; ".join(outcome.rejected_because or []),
            )

        assert outcome.result is not None
        return HoldoutLaneResult(
            lane_run=LaneRun(
                lane=Lane.HIDDEN,
                candidate_commit=binding.commit_sha,
                verdict=outcome.result.verdict,
                detail=f"oracle_version={outcome.result.oracle_version}",
            ),
            outcome=outcome,
        )
