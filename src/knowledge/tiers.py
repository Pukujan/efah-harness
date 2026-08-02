"""Knowledge tiers T0..T7 and the promotion rules that guard them.

Contract Section 15.5: *unverified agent output MUST NOT be presented as
trusted knowledge.* GATE-D2-18 turns that sentence into four assertions, and
this module is where they are enforced rather than described.

The failure mode is quiet and cumulative. An agent produces a plausible claim;
nothing marks it as unverified; a later agent retrieves it as established fact
and builds on it. By the time anyone checks, the original claim is three
inferences deep and the provenance is gone. Tiers exist so that never happens
silently: a claim carries its own tier, and :func:`is_trusted` is the only way
to ask whether it may be relied on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governance.envelope import (
    TRUSTED_KNOWLEDGE_FLOOR,
    CompiledObject,
    KnowledgeTier,
    utc_now,
)
from governance.states import DriftFinding, TaskState

#: A refusal names the contract's own typed finding. ROLE_CONFLICT is a Section
#: 19.2 drift finding, not a task state, so both enumerations appear here.
FailureState = TaskState | DriftFinding

#: Contract Section 15.5, weakest first. Index is the tier's rank.
TIER_ORDER: tuple[KnowledgeTier, ...] = (
    KnowledgeTier.T0_RAW,
    KnowledgeTier.T1_OBSERVATION,
    KnowledgeTier.T2_HYPOTHESIS,
    KnowledgeTier.T3_TESTED,
    KnowledgeTier.T4_REPRODUCIBLE,
    KnowledgeTier.T5_INDEPENDENTLY_VERIFIED,
    KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE,
    KnowledgeTier.T7_HARD_GOLD,
)

#: GATE-D2-18 A1. Agent-generated content enters at or below T2_HYPOTHESIS.
MAX_TIER_FOR_AGENT_OUTPUT = KnowledgeTier.T2_HYPOTHESIS

#: GATE-D2-18 A2. Above this, a different vendor family must have verified it.
INDEPENDENT_VERIFICATION_REQUIRED_ABOVE = KnowledgeTier.T4_REPRODUCIBLE

#: Contract Section 15.6. All five, recorded, or no hard-gold promotion.
GOLD_PROMOTION_STEPS: tuple[str, ...] = (
    "quarantine",
    "reproducibility",
    "independent_verification",
    "mutant_validation",
    "contamination_review",
)


def rank(tier: KnowledgeTier) -> int:
    return TIER_ORDER.index(tier)


def is_trusted(tier: KnowledgeTier) -> bool:
    """Section 15.5. Below the floor, it may be used but not presented as trusted."""
    return rank(tier) >= rank(TRUSTED_KNOWLEDGE_FLOOR)


class PromotionRejected(RuntimeError):
    """A promotion that Section 15.5/15.6 does not permit."""


@dataclass
class Verification:
    """One independent check of a knowledge item."""

    verifier_alias: str
    verifier_family: str
    method: str
    passed: bool
    evidence_ref: str | None = None


@dataclass
class KnowledgeItem:
    item_id: str
    statement: str
    tier: KnowledgeTier
    producer_alias: str
    producer_family: str
    produced_by_agent: bool = True
    verifications: list[Verification] = field(default_factory=list)
    reproduction_runs: int = 0
    gold_steps_recorded: set[str] = field(default_factory=set)
    created_at: str = field(default_factory=utc_now)

    @property
    def trusted(self) -> bool:
        return is_trusted(self.tier)

    def independent_verifications(self) -> list[Verification]:
        """Section 12.2: a different family, and it has to have passed."""
        return [
            v
            for v in self.verifications
            if v.passed and v.verifier_family != self.producer_family
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "tier": self.tier.value,
            "trusted": self.trusted,
            "producer_alias": self.producer_alias,
            "producer_family": self.producer_family,
            "produced_by_agent": self.produced_by_agent,
            "independent_verification_count": len(self.independent_verifications()),
            "reproduction_runs": self.reproduction_runs,
            "gold_steps_recorded": sorted(self.gold_steps_recorded),
        }


@dataclass
class PromotionOutcome:
    item_id: str
    from_tier: KnowledgeTier
    to_tier: KnowledgeTier
    allowed: bool
    blockers: list[str] = field(default_factory=list)
    failure_state: FailureState | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "promotion_path_assert",
            "item_id": self.item_id,
            "from_tier": self.from_tier.value,
            "to_tier": self.to_tier.value,
            "allowed": self.allowed,
            "blockers": self.blockers,
            "failure_state": self.failure_state.value if self.failure_state else None,
        }


def admit_agent_output(
    *,
    item_id: str,
    statement: str,
    producer_alias: str,
    producer_family: str,
    claimed_tier: KnowledgeTier = KnowledgeTier.T2_HYPOTHESIS,
) -> KnowledgeItem:
    """GATE-D2-18 A1. Agent output enters at or below T2, whatever it claims.

    The claim is *clamped*, not rejected, and the clamping is the point: an
    agent asserting its own output is verified must not be able to make that
    true by asserting it harder.
    """
    entry = claimed_tier if rank(claimed_tier) <= rank(MAX_TIER_FOR_AGENT_OUTPUT) else MAX_TIER_FOR_AGENT_OUTPUT
    return KnowledgeItem(
        item_id=item_id,
        statement=statement,
        tier=entry,
        producer_alias=producer_alias,
        producer_family=producer_family,
        produced_by_agent=True,
    )


def evaluate_promotion(item: KnowledgeItem, to_tier: KnowledgeTier) -> PromotionOutcome:
    """Decide a promotion without performing it. Deterministic, no model call."""
    blockers: list[str] = []
    failure_state: FailureState | None = None

    if rank(to_tier) <= rank(item.tier):
        blockers.append(f"{to_tier.value} is not above the current tier {item.tier.value}")

    if rank(to_tier) >= rank(KnowledgeTier.T3_TESTED) and not any(
        v.passed for v in item.verifications
    ):
        blockers.append("no passing verification recorded; T3_TESTED requires a test")
        failure_state = TaskState.FAILED_ORACLE

    if rank(to_tier) >= rank(KnowledgeTier.T4_REPRODUCIBLE) and item.reproduction_runs < 2:
        blockers.append(
            f"T4_REPRODUCIBLE requires at least two independent reproductions, "
            f"found {item.reproduction_runs}"
        )
        failure_state = failure_state or TaskState.FAILED_ORACLE

    # A2: above T4, the verifier must be a different vendor family.
    if rank(to_tier) > rank(INDEPENDENT_VERIFICATION_REQUIRED_ABOVE) and (
        not item.independent_verifications()
    ):
        blockers.append(
            f"promotion above {INDEPENDENT_VERIFICATION_REQUIRED_ABOVE.value} requires a "
            f"passing verification from a family other than {item.producer_family!r}"
        )
        failure_state = DriftFinding.ROLE_CONFLICT

    # A3/A4: T6 and T7 need the whole Section 15.6 path.
    if rank(to_tier) >= rank(KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE):
        missing = [s for s in GOLD_PROMOTION_STEPS if s not in item.gold_steps_recorded]
        if missing:
            blockers.append(f"Section 15.6 promotion steps not recorded: {missing}")
            failure_state = TaskState.FAILED_ORACLE

    return PromotionOutcome(
        item_id=item.item_id,
        from_tier=item.tier,
        to_tier=to_tier,
        allowed=not blockers,
        blockers=blockers,
        failure_state=None if not blockers else (failure_state or TaskState.FAILED_ORACLE),
    )


def promote(item: KnowledgeItem, to_tier: KnowledgeTier) -> PromotionOutcome:
    """Apply a promotion, or refuse it. Refusal raises; the caller cannot ignore it."""
    outcome = evaluate_promotion(item, to_tier)
    if not outcome.allowed:
        raise PromotionRejected(
            f"{item.item_id}: cannot promote {item.tier.value} -> {to_tier.value}: "
            + "; ".join(outcome.blockers)
        )
    item.tier = to_tier
    return outcome


def presentation_label(item: KnowledgeItem) -> str:
    """What a consumer is allowed to call this. GATE-D2-18 A1 in one string."""
    if item.trusted:
        return "TRUSTED"
    return f"UNVERIFIED ({item.tier.value})"


def to_compiled_object(item: KnowledgeItem) -> CompiledObject:
    return CompiledObject.create(
        schema_id="efah.knowledge_item",
        created_by_alias=item.producer_alias,
        body=item.as_dict() | {"presentation": presentation_label(item)},
    )
