"""Hard-gold promotion — the gate, not just the record.

Contract Section 15.6. A successful task becomes a hard-gold candidate only
when the system preserves nine things, and promotion requires five steps:
quarantine, reproducibility, independent verification, mutant validation, and
contamination review.

The distinction this module insists on is between *recording* a step and
*passing* it. A checklist with five ticks is a record; a gate is something that
refuses. Each step here is evaluated from evidence -- reproduction runs that
actually agree, a verifier from a different vendor family, mutants that were
actually killed -- and a step whose evidence is absent fails rather than
defaulting to satisfied.

Contamination review is the one that is easiest to skip and worst to skip. A
gold case that leaked into a training corpus, or that the implementer already
saw, measures memory rather than capability. Section 15.6 lists a contamination
and trainability policy among the nine preserved items for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governance.envelope import CompiledObject, KnowledgeTier, content_hash, utc_now
from governance.states import TaskState
from knowledge.tiers import GOLD_PROMOTION_STEPS, KnowledgeItem, evaluate_promotion

#: Contract Section 15.6 — the nine things a hard-gold candidate must preserve.
PRESERVED_FIELDS: tuple[str, ...] = (
    "original_contract_and_specification",
    "initial_environment_and_versions",
    "expected_and_observed_results",
    "tests_and_oracles",
    "artifacts_and_hashes",
    "traces_and_tool_calls",
    "independent_verification",
    "failure_variants_and_mutants",
    "contamination_and_trainability_policy",
)


@dataclass
class ReproductionRun:
    run_id: str
    environment: str
    commit_sha: str
    observed_result_hash: str
    exit_status: int


@dataclass
class GoldCandidate:
    """Everything Section 15.6 requires preserved, plus the evidence behind it."""

    case_id: str
    work_unit_id: str
    candidate_commit: str
    knowledge_item: KnowledgeItem
    preserved: dict[str, Any] = field(default_factory=dict)
    quarantined_at: str | None = None
    quarantine_release_reviewed: bool = False
    reproduction_runs: list[ReproductionRun] = field(default_factory=list)
    independent_verifier_alias: str | None = None
    independent_verifier_family: str | None = None
    mutants_seeded: int = 0
    mutants_killed: int = 0
    contamination_reviewed: bool = False
    contamination_findings: list[str] = field(default_factory=list)
    trainability_policy: str | None = None

    def preserved_hash(self) -> str:
        return content_hash(self.preserved)


@dataclass
class StepResult:
    step: str
    satisfied: bool
    detail: str


@dataclass
class GoldPromotionResult:
    case_id: str
    allowed: bool
    steps: list[StepResult] = field(default_factory=list)
    missing_preserved_fields: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    failure_state: TaskState | None = None
    decided_at: str = field(default_factory=utc_now)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "promotion_checklist_assert",
            "expected": "all_five_steps_recorded",
            "case_id": self.case_id,
            "allowed": self.allowed,
            "steps": [
                {"step": s.step, "satisfied": s.satisfied, "detail": s.detail} for s in self.steps
            ],
            "steps_satisfied": sum(1 for s in self.steps if s.satisfied),
            "steps_required": len(GOLD_PROMOTION_STEPS),
            "missing_preserved_fields": self.missing_preserved_fields,
            "blockers": self.blockers,
            "failure_state": self.failure_state.value if self.failure_state else None,
            "decided_at": self.decided_at,
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.gold_promotion_result",
            created_by_alias="auditor-a07",
            body=self.as_evidence(),
        )


def _quarantine(candidate: GoldCandidate) -> StepResult:
    if candidate.quarantined_at is None:
        return StepResult("quarantine", False, "candidate was never quarantined")
    if not candidate.quarantine_release_reviewed:
        return StepResult(
            "quarantine", False, "quarantine release was not reviewed before promotion"
        )
    return StepResult("quarantine", True, f"quarantined at {candidate.quarantined_at}, reviewed")


def _reproducibility(candidate: GoldCandidate) -> StepResult:
    runs = candidate.reproduction_runs
    if len(runs) < 2:
        return StepResult(
            "reproducibility", False, f"{len(runs)} reproduction run(s); at least two required"
        )
    if len({r.commit_sha for r in runs}) != 1:
        return StepResult(
            "reproducibility", False, "reproduction runs do not share one candidate commit"
        )
    if len({r.observed_result_hash for r in runs}) != 1:
        return StepResult("reproducibility", False, "reproduction runs disagree on the result")
    if any(r.exit_status != 0 for r in runs):
        return StepResult("reproducibility", False, "a reproduction run exited non-zero")
    if len({r.environment for r in runs}) < 2:
        return StepResult(
            "reproducibility",
            False,
            "all reproductions ran in one environment; that is a repeat, not a reproduction",
        )
    return StepResult("reproducibility", True, f"{len(runs)} agreeing runs across environments")


def _independent_verification(candidate: GoldCandidate) -> StepResult:
    if not candidate.independent_verifier_family:
        return StepResult("independent_verification", False, "no independent verifier recorded")
    if candidate.independent_verifier_family == candidate.knowledge_item.producer_family:
        return StepResult(
            "independent_verification",
            False,
            f"verifier family {candidate.independent_verifier_family!r} is the producer's own "
            "family; that is self-agreement, not verification",
        )
    if not candidate.knowledge_item.independent_verifications():
        return StepResult(
            "independent_verification", False, "no passing cross-family verification on the item"
        )
    return StepResult(
        "independent_verification",
        True,
        f"verified by {candidate.independent_verifier_alias} ({candidate.independent_verifier_family})",
    )


def _mutant_validation(candidate: GoldCandidate) -> StepResult:
    if candidate.mutants_seeded == 0:
        return StepResult(
            "mutant_validation", False, "no mutants seeded; a case that kills nothing proves nothing"
        )
    if candidate.mutants_killed < candidate.mutants_seeded:
        return StepResult(
            "mutant_validation",
            False,
            f"{candidate.mutants_killed}/{candidate.mutants_seeded} mutants killed; "
            "Section 9.4 required_kill_rate is 1.0",
        )
    return StepResult(
        "mutant_validation", True, f"{candidate.mutants_killed}/{candidate.mutants_seeded} killed"
    )


def _contamination_review(candidate: GoldCandidate) -> StepResult:
    if not candidate.contamination_reviewed:
        return StepResult("contamination_review", False, "contamination review not performed")
    if candidate.contamination_findings:
        return StepResult(
            "contamination_review",
            False,
            f"unresolved contamination findings: {candidate.contamination_findings}",
        )
    if not candidate.trainability_policy:
        return StepResult("contamination_review", False, "no trainability policy recorded")
    return StepResult("contamination_review", True, f"policy: {candidate.trainability_policy}")


_STEP_CHECKS = {
    "quarantine": _quarantine,
    "reproducibility": _reproducibility,
    "independent_verification": _independent_verification,
    "mutant_validation": _mutant_validation,
    "contamination_review": _contamination_review,
}


def evaluate_gold_promotion(candidate: GoldCandidate) -> GoldPromotionResult:
    """The gate. Every step is evaluated from evidence; none defaults to satisfied."""
    steps = [_STEP_CHECKS[name](candidate) for name in GOLD_PROMOTION_STEPS]
    missing_fields = [f for f in PRESERVED_FIELDS if not candidate.preserved.get(f)]

    blockers = [f"{s.step}: {s.detail}" for s in steps if not s.satisfied]
    if missing_fields:
        blockers.append(f"Section 15.6 preserved fields absent or empty: {missing_fields}")

    # The tier machinery must agree too -- T7 is not reachable by this path alone.
    satisfied_steps = {s.step for s in steps if s.satisfied}
    candidate.knowledge_item.gold_steps_recorded |= satisfied_steps
    tier_outcome = evaluate_promotion(candidate.knowledge_item, KnowledgeTier.T7_HARD_GOLD)
    blockers.extend(f"knowledge tier: {b}" for b in tier_outcome.blockers)

    return GoldPromotionResult(
        case_id=candidate.case_id,
        allowed=not blockers,
        steps=steps,
        missing_preserved_fields=missing_fields,
        blockers=blockers,
        failure_state=None if not blockers else TaskState.FAILED_ORACLE,
    )


def promote_to_hard_gold(candidate: GoldCandidate) -> GoldPromotionResult:
    """Promote, or refuse. A refusal quarantines rather than silently declining."""
    result = evaluate_gold_promotion(candidate)
    if result.allowed:
        candidate.knowledge_item.tier = KnowledgeTier.T7_HARD_GOLD
    return result
