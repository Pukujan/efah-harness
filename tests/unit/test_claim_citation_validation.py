"""§7.3 source assurance and §15.4 citation validation.

The property under test is narrow and load-bearing: **a citation whose quote is
not in its source must fail, deterministically, with no model in the verdict
path.** Everything else here exists to stop that check being weakened by
accident — by fuzzy matching, by treating a missing file as a lie, by letting a
model's own assertion count as evidence for itself, or by letting an author
mark a load-bearing claim as incidental to dodge the requirement.

The fabrication tests are written against the real contract file, because a
hallucinated citation to a document that exists is the realistic attack. A test
that only ever cites a temp file would pass while the mechanism was broken for
every source anyone actually cites.
"""

from __future__ import annotations

import hashlib

import pytest

from governance.envelope import KnowledgeTier
from research.claims import (
    MINIMUM_AUTHORITY_FOR_LOAD_BEARING,
    Citation,
    CitationStatus,
    Claim,
    ClaimVerdict,
    SourceClass,
    SourceUnreadable,
    SupportKind,
    authority_rank,
    cite_repo_file,
    validate_claim,
    verify_citation,
)

CONTRACT = "project-pack/contract.md"
#: A sentence that really is in §7.3, quoted exactly.
REAL_QUOTE = "Every load-bearing claim MUST record"
#: Plausible, contract-flavoured, and nowhere in the file.
FABRICATED_QUOTE = (
    "Every load-bearing claim MUST be reviewed by a second agent within 24 hours "
    "and recorded in the citation registry."
)


def _hash_of(path: str) -> str:
    from research.claims import REPO_ROOT

    return "sha256:" + hashlib.sha256((REPO_ROOT / path).read_text(errors="replace").encode()).hexdigest()


def _citation(**overrides) -> Citation:
    base = {
        "source_id": "CONTRACT-7.3",
        "source_pointer": CONTRACT,
        "source_class": SourceClass.RECORDED_DECISION,
        "retrieved_at": "2026-08-02",
        "exact_location": "§7.3 Source assurance",
        "quote": REAL_QUOTE,
        "support_kind": SupportKind.DIRECT,
        "applicability": "EFAH-CONTRACT-001 v1.1, the contract this build executes",
        "content_hash": _hash_of(CONTRACT),
        "retrieval_provenance": "read from the working tree",
    }
    return Citation(**{**base, **overrides})


# -- the check that matters ------------------------------------------------
def test_a_real_quote_verifies():
    check = verify_citation(_citation())
    assert check.status is CitationStatus.VERIFIED
    assert check.counts_as_support


def test_a_fabricated_quote_is_unsupported():
    """The hallucination signal. A plausible sentence that is simply not there."""
    check = verify_citation(_citation(quote=FABRICATED_QUOTE))
    assert check.status is CitationStatus.UNSUPPORTED
    assert not check.counts_as_support
    assert "does not occur" in check.detail


def test_a_claim_resting_on_a_fabricated_citation_is_unsupported():
    claim = Claim(
        claim_id="C-1",
        statement="the contract requires second-agent review of every claim",
        citations=[_citation(quote=FABRICATED_QUOTE)],
        affected_requirement="REQ-CITATION",
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.UNSUPPORTED
    assert not result.supported
    assert result.max_knowledge_tier is KnowledgeTier.T2_HYPOTHESIS


def test_quote_matching_survives_reflow_but_not_rewording():
    """Whitespace is normalised; words are not.

    "MUST NOT" and "must" are the difference between a clause and its opposite,
    so no fuzzy ratio, no case folding, no punctuation stripping.
    """
    reflowed = verify_citation(_citation(quote="Every load-bearing\n   claim   MUST record"))
    assert reflowed.status is CitationStatus.VERIFIED

    reworded = verify_citation(_citation(quote="Every load-bearing claim should record"))
    assert reworded.status is CitationStatus.UNSUPPORTED

    recased = verify_citation(_citation(quote="every load-bearing claim must record"))
    assert recased.status is CitationStatus.UNSUPPORTED


# -- the three failure modes stay distinct ---------------------------------
def test_a_missing_source_is_unresolvable_not_unsupported():
    """A deleted file is an infrastructure problem, not a lie."""
    check = verify_citation(_citation(source_pointer="does/not/exist.md"))
    assert check.status is CitationStatus.UNRESOLVABLE


def test_a_changed_source_is_stale_not_unsupported():
    check = verify_citation(_citation(content_hash="sha256:" + "0" * 64))
    assert check.status is CitationStatus.STALE
    assert "changed since retrieval" in check.detail


def test_a_fabricated_quote_in_a_changed_source_reports_fabrication():
    """The interesting failure must not hide behind the boring one."""
    check = verify_citation(
        _citation(quote=FABRICATED_QUOTE, content_hash="sha256:" + "0" * 64)
    )
    assert check.status is CitationStatus.UNSUPPORTED


# -- §7.3 structural completeness ------------------------------------------
@pytest.mark.parametrize(
    "field",
    ["source_id", "source_pointer", "retrieved_at", "exact_location", "quote",
     "applicability", "content_hash", "retrieval_provenance"],
)
def test_every_required_source_assurance_field_is_enforced(field):
    check = verify_citation(_citation(**{field: ""}))
    assert check.status is CitationStatus.MALFORMED
    assert field in check.detail


def test_an_inference_must_state_its_step():
    """An unstated inference is where a false claim hides behind a true source."""
    check = verify_citation(_citation(support_kind=SupportKind.INFERENCE))
    assert check.status is CitationStatus.MALFORMED
    assert "inference_step" in check.detail

    stated = verify_citation(
        _citation(
            support_kind=SupportKind.INFERENCE,
            inference_step="the clause names load-bearing claims, and this claim is one",
        )
    )
    assert stated.status is CitationStatus.VERIFIED


def test_a_content_hash_must_be_a_real_digest():
    check = verify_citation(_citation(content_hash="whatever"))
    assert check.status is CitationStatus.MALFORMED


# -- §15.4 INSUFFICIENT_EVIDENCE is returnable -----------------------------
def test_an_uncited_load_bearing_claim_is_insufficient_evidence_not_false():
    """§15.4: "The retriever MUST be able to return INSUFFICIENT_EVIDENCE"."""
    claim = Claim(claim_id="C-2", statement="the gateway retries five times", citations=[])
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "no citation" in result.findings[0]


def test_a_model_assertion_alone_is_not_evidence():
    """§15.5: unverified agent output must not be presented as trusted knowledge."""
    claim = Claim(
        claim_id="C-3",
        statement="the eval gateway is DB-less",
        citations=[
            _citation(
                source_id="MODEL-1",
                source_class=SourceClass.MODEL_ASSERTION,
                quote=REAL_QUOTE,
            )
        ],
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert any("MODEL_ASSERTION" in f for f in result.findings)


def test_a_model_assertion_ranks_below_the_load_bearing_floor():
    assert authority_rank(SourceClass.MODEL_ASSERTION) > authority_rank(
        MINIMUM_AUTHORITY_FOR_LOAD_BEARING
    )


def test_inference_alone_does_not_carry_a_load_bearing_claim():
    claim = Claim(
        claim_id="C-4",
        statement="therefore every worker must cite",
        citations=[
            _citation(
                support_kind=SupportKind.INFERENCE,
                inference_step="load-bearing claims include worker output",
            )
        ],
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert any("INFERENCE" in f for f in result.findings)


# -- the obvious dodge -----------------------------------------------------
def test_naming_an_affected_requirement_makes_a_claim_load_bearing():
    """Marking a load-bearing claim incidental is the obvious way around this."""
    claim = Claim(
        claim_id="C-5",
        statement="the router enforces family separation",
        citations=[],
        load_bearing=False,
        affected_requirement="REQ-012",
    )
    assert claim.is_load_bearing
    assert validate_claim(claim).verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE


def test_a_genuinely_incidental_uncited_claim_is_allowed():
    claim = Claim(claim_id="C-6", statement="this run took a while", citations=[], load_bearing=False)
    assert validate_claim(claim).verdict is ClaimVerdict.SUPPORTED


# -- tier integration ------------------------------------------------------
def test_a_supported_claim_may_reach_t4_and_no_further():
    """Above T4 needs cross-family independent verification (GATE-D2-18 A2).

    Citation validation is not independent verification, so it cannot buy a
    tier that rule guards.
    """
    claim = Claim(claim_id="C-7", statement="§7.3 requires a source record", citations=[_citation()])
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.SUPPORTED
    assert result.max_knowledge_tier is KnowledgeTier.T4_REPRODUCIBLE


def test_a_stale_claim_drops_to_observation():
    claim = Claim(
        claim_id="C-8",
        statement="§7.3 requires a source record",
        citations=[_citation(content_hash="sha256:" + "1" * 64)],
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.STALE
    assert result.max_knowledge_tier is KnowledgeTier.T1_OBSERVATION


# -- no judge in the verdict path ------------------------------------------
def test_the_verdict_path_declares_and_contains_no_model_judge():
    claim = Claim(claim_id="C-9", statement="x", citations=[_citation()])
    body = validate_claim(claim).as_body()
    assert body["model_judge_in_verdict_path"] is False
    assert body["oracle_type"] == "exact_deterministic_execution_or_state"


def test_validation_is_a_compiled_object_with_an_envelope():
    claim = Claim(claim_id="C-10", statement="x", citations=[_citation()])
    dumped = validate_claim(claim).to_compiled_object().model_dump(mode="json")
    assert dumped["envelope"]["schema_id"] == "efah.claim_validation"


# -- the convenience constructor hashes rather than trusts ------------------
def test_cite_repo_file_hashes_the_file_itself():
    """A caller-supplied hash is a caller-supplied claim, which is the thing
    being checked."""
    citation = cite_repo_file(
        source_id="C7.3",
        path=CONTRACT,
        quote=REAL_QUOTE,
        exact_location="§7.3",
        applicability="this contract",
    )
    assert citation.content_hash == _hash_of(CONTRACT)
    assert verify_citation(citation).status is CitationStatus.VERIFIED


def test_reading_a_missing_source_raises_rather_than_returning_empty():
    with pytest.raises(SourceUnreadable):
        cite_repo_file(
            source_id="X",
            path="nope.md",
            quote="x",
            exact_location="y",
            applicability="z",
        )
