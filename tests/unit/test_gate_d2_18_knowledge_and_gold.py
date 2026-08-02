"""Knowledge tiers and hard-gold promotion.

Contract Sections 15.5, 15.6 · GATE-D2-18. The property: unverified agent
output must not be presented as trusted, and hard gold requires the whole
five-step path rather than a checklist somebody ticked.
"""

from __future__ import annotations

import pytest

from gold.promotion import (
    PRESERVED_FIELDS,
    GoldCandidate,
    ReproductionRun,
    evaluate_gold_promotion,
    promote_to_hard_gold,
)
from governance.envelope import TRUSTED_KNOWLEDGE_FLOOR, KnowledgeTier
from governance.states import DriftFinding
from knowledge.tiers import (
    CITATION_VERDICT_SUPPORTED,
    GOLD_PROMOTION_STEPS,
    PromotionRejected,
    Verification,
    admit_agent_output,
    evaluate_promotion,
    is_trusted,
    presentation_label,
    promote,
    rank,
)


def _verified_item(item_id: str = "K-1"):
    item = admit_agent_output(
        item_id=item_id,
        statement="the eval gateway performs zero retries",
        producer_alias="researcher-r17",
        producer_family="openai",
    )
    item.reproduction_runs = 2
    item.verifications = [Verification("critic-c08", "xai", "independent rerun", True)]
    # FINDING-007: promotion above T2 now also requires that the statement's
    # §7.3 citations were validated and held. This fixture exists to exercise
    # the *other* promotion rules, so it satisfies the citation gate explicitly
    # rather than by omission — an item with no verdict is blocked, and
    # test_an_item_whose_citations_were_never_checked_cannot_be_promoted covers
    # that path directly.
    item.citation_verdict = CITATION_VERDICT_SUPPORTED
    return item


# --- A1: agent output enters at or below T2 -------------------------------

@pytest.mark.parametrize("claimed", list(KnowledgeTier))
def test_agent_output_never_enters_above_t2(claimed):
    item = admit_agent_output(
        item_id="K-x",
        statement="x",
        producer_alias="researcher-r17",
        producer_family="openai",
        claimed_tier=claimed,
    )
    assert rank(item.tier) <= rank(KnowledgeTier.T2_HYPOTHESIS)
    assert not item.trusted
    assert presentation_label(item).startswith("UNVERIFIED")


def test_the_trusted_floor_is_t6():
    assert TRUSTED_KNOWLEDGE_FLOOR is KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE
    assert not is_trusted(KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)
    assert is_trusted(KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE)
    assert is_trusted(KnowledgeTier.T7_HARD_GOLD)


# --- A2: above T4 needs a different family --------------------------------

def test_same_family_verification_does_not_promote_above_t4():
    item = _verified_item()
    item.verifications = [Verification("implementer-i12", "openai", "rerun", True)]
    outcome = evaluate_promotion(item, KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)
    assert not outcome.allowed
    assert outcome.failure_state is DriftFinding.ROLE_CONFLICT


def test_cross_family_verification_promotes_above_t4():
    outcome = evaluate_promotion(_verified_item(), KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)
    assert outcome.allowed, outcome.blockers


def test_a_failed_cross_family_verification_does_not_count():
    item = _verified_item()
    item.verifications = [Verification("critic-c08", "xai", "independent rerun", False)]
    outcome = evaluate_promotion(item, KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)
    assert not outcome.allowed


def test_a_single_reproduction_is_not_reproducibility():
    item = _verified_item()
    item.reproduction_runs = 1
    outcome = evaluate_promotion(item, KnowledgeTier.T4_REPRODUCIBLE)
    assert not outcome.allowed


# --- A3/A4: hard gold needs the full path ---------------------------------

def test_t6_and_t7_require_all_five_promotion_steps():
    item = _verified_item()
    for tier in (KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE, KnowledgeTier.T7_HARD_GOLD):
        assert not evaluate_promotion(item, tier).allowed
    item.gold_steps_recorded = set(GOLD_PROMOTION_STEPS)
    assert evaluate_promotion(item, KnowledgeTier.T7_HARD_GOLD).allowed


@pytest.mark.parametrize("omitted", GOLD_PROMOTION_STEPS)
def test_omitting_any_one_step_blocks_promotion(omitted):
    item = _verified_item()
    item.gold_steps_recorded = set(GOLD_PROMOTION_STEPS) - {omitted}
    outcome = evaluate_promotion(item, KnowledgeTier.T7_HARD_GOLD)
    assert not outcome.allowed
    assert omitted in str(outcome.blockers)


def test_promote_raises_rather_than_returning_a_soft_refusal():
    item = admit_agent_output(
        item_id="K-2", statement="x", producer_alias="researcher-r17", producer_family="openai"
    )
    with pytest.raises(PromotionRejected):
        promote(item, KnowledgeTier.T7_HARD_GOLD)
    assert item.tier is KnowledgeTier.T2_HYPOTHESIS


# --- the gold gate itself -------------------------------------------------

def _gold_candidate(**overrides) -> GoldCandidate:
    candidate = GoldCandidate(
        case_id="GOLD-001",
        work_unit_id="WU-040",
        candidate_commit="a" * 40,
        knowledge_item=_verified_item("K-gold"),
        preserved={field: f"recorded:{field}" for field in PRESERVED_FIELDS},
        quarantined_at="2026-08-02T00:00:00Z",
        quarantine_release_reviewed=True,
        reproduction_runs=[
            ReproductionRun("run-1", "linux/py3.12", "a" * 40, "sha256:beef", 0),
            ReproductionRun("run-2", "linux/py3.13", "a" * 40, "sha256:beef", 0),
        ],
        independent_verifier_alias="critic-c08",
        independent_verifier_family="xai",
        mutants_seeded=4,
        mutants_killed=4,
        contamination_reviewed=True,
        trainability_policy="not_trainable_until_release",
    )
    for key, value in overrides.items():
        setattr(candidate, key, value)
    return candidate


def test_a_complete_gold_candidate_is_promoted():
    result = promote_to_hard_gold(_gold_candidate())
    assert result.allowed, result.blockers
    assert all(step.satisfied for step in result.steps)


def test_reproductions_in_one_environment_are_a_repeat_not_a_reproduction():
    candidate = _gold_candidate(
        reproduction_runs=[
            ReproductionRun("run-1", "linux/py3.12", "a" * 40, "sha256:beef", 0),
            ReproductionRun("run-2", "linux/py3.12", "a" * 40, "sha256:beef", 0),
        ]
    )
    result = evaluate_gold_promotion(candidate)
    assert not result.allowed
    assert any("one environment" in b for b in result.blockers)


def test_disagreeing_reproductions_block_promotion():
    candidate = _gold_candidate(
        reproduction_runs=[
            ReproductionRun("run-1", "linux/py3.12", "a" * 40, "sha256:beef", 0),
            ReproductionRun("run-2", "linux/py3.13", "a" * 40, "sha256:cafe", 0),
        ]
    )
    assert not evaluate_gold_promotion(candidate).allowed


def test_a_surviving_mutant_blocks_promotion():
    candidate = _gold_candidate(mutants_seeded=4, mutants_killed=3)
    result = evaluate_gold_promotion(candidate)
    assert not result.allowed
    assert any("required_kill_rate" in b for b in result.blockers)


def test_same_family_verifier_is_self_agreement_not_verification():
    candidate = _gold_candidate(independent_verifier_family="openai")
    assert not evaluate_gold_promotion(candidate).allowed


def test_unresolved_contamination_findings_block_promotion():
    candidate = _gold_candidate(contamination_findings=["case appears in a public corpus"])
    result = evaluate_gold_promotion(candidate)
    assert not result.allowed
    assert any("contamination" in b for b in result.blockers)


@pytest.mark.parametrize("omitted", PRESERVED_FIELDS)
def test_every_section_15_6_preserved_field_is_required(omitted):
    candidate = _gold_candidate()
    candidate.preserved.pop(omitted)
    result = evaluate_gold_promotion(candidate)
    assert not result.allowed
    assert omitted in result.missing_preserved_fields


def test_a_refused_promotion_leaves_the_tier_untouched():
    candidate = _gold_candidate(quarantined_at=None)
    before = candidate.knowledge_item.tier
    result = promote_to_hard_gold(candidate)
    assert not result.allowed
    assert candidate.knowledge_item.tier is before


# --- FINDING-007: citation validation gates promotion above T2 -------------
#
# Tiers already stopped unverified agent output being *presented* as trusted.
# They did not stop it being *fabricated*, because nothing checked that a cited
# source says what the citation claims it says. These tests pin the coupling.

def test_an_item_whose_citations_were_never_checked_cannot_be_promoted():
    """An absent verdict is not a passing one.

    This is the FINDING-004 error in a different place: counting a missing
    signal as success. An item nobody checked must block, not sail through.
    """
    item = _verified_item()
    item.citation_verdict = None
    outcome = evaluate_promotion(item, KnowledgeTier.T3_TESTED)
    assert not outcome.allowed
    assert any("citation validation has not been run" in b for b in outcome.blockers)


@pytest.mark.parametrize(
    "verdict", ["INSUFFICIENT_EVIDENCE", "UNSUPPORTED", "STALE"]
)
def test_a_claim_its_sources_do_not_support_cannot_become_knowledge(verdict):
    item = _verified_item()
    item.citation_verdict = verdict
    outcome = evaluate_promotion(item, KnowledgeTier.T3_TESTED)
    assert not outcome.allowed
    assert any(verdict in b for b in outcome.blockers)


def test_citation_validation_does_not_gate_at_or_below_t2():
    """T2 is where a hypothesis lives. A hypothesis is allowed to be uncited —
    it just may not be called knowledge."""
    item = admit_agent_output(
        item_id="K-hyp",
        statement="the gateway might retry",
        producer_alias="researcher-r17",
        producer_family="openai",
        claimed_tier=KnowledgeTier.T0_RAW,
    )
    item.citation_verdict = None
    outcome = evaluate_promotion(item, KnowledgeTier.T2_HYPOTHESIS)
    assert not any("citation" in b for b in outcome.blockers)


def test_the_supported_verdict_string_matches_the_research_plane():
    """The two modules are deliberately decoupled; this stops them drifting.

    knowledge.tiers spells the verdict as a literal so it does not depend on
    research.claims. That decoupling is only safe if something pins the string.
    """
    from research.claims import ClaimVerdict

    assert ClaimVerdict.SUPPORTED.value == CITATION_VERDICT_SUPPORTED


def test_a_validated_claim_flows_into_a_promotable_item():
    """End to end: validate a real citation, carry the verdict, promote."""
    from research.claims import Claim, cite_repo_file, validate_claim

    citation = cite_repo_file(
        source_id="CONTRACT-15.5",
        path="project-pack/contract.md",
        quote="Unverified agent output MUST NOT be presented as trusted knowledge",
        exact_location="§15.5 Knowledge tiers",
        applicability="EFAH-CONTRACT-001 v1.1",
    )
    validation = validate_claim(
        Claim(
            claim_id="C-TIER",
            statement="unverified agent output is not trusted knowledge",
            citations=[citation],
            affected_requirement="REQ-KNOWLEDGE-TIERS",
        )
    )
    assert validation.supported

    item = _verified_item("K-flow")
    item.citation_verdict = validation.verdict.value
    assert evaluate_promotion(item, KnowledgeTier.T3_TESTED).allowed
