"""§7.3 source assurance and §15.4 citation validation.

The property under test is narrow and load-bearing: **a citation whose quote is
not in its source, or not where the citation says it is, must fail
deterministically, with no model in the verdict path.** Everything else here
exists to stop that check being weakened by accident — by fuzzy matching, by
treating a missing file as a lie, by letting a model's own assertion count as
evidence for itself, or by letting an author mark a load-bearing claim as
incidental to dodge the requirement.

The fabrication tests are written against the real contract file, because a
hallucinated citation to a document that exists is the realistic attack. A test
that only ever cites a temp file would pass while the mechanism was broken for
every source anyone actually cites.

The gaming probe at the top of this file is the test the module failed on
2026-08-03: a flatly false load-bearing statement, cited with the word "the" at
a section that does not exist, came back SUPPORTED at T4 with no findings. Every
hardening below is one of the reasons that probe now fails.
"""

from __future__ import annotations

import hashlib

import pytest

from governance.envelope import KnowledgeTier
from research.claims import (
    LOCATION_GRAMMAR,
    MAXIMUM_LOCATION_LINES,
    MINIMUM_AUTHORITY_FOR_LOAD_BEARING,
    MINIMUM_QUOTE_CHARACTERS,
    Citation,
    CitationStatus,
    Claim,
    ClaimVerdict,
    SourceClass,
    SourceUnreadable,
    SupportKind,
    authority_rank,
    cite_repo_file,
    content_tokens,
    decode_source,
    locate,
    parse_location,
    read_source_bytes,
    statement_quote_overlap,
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
    """The hash of the file's raw bytes, which is what the validator computes."""
    from research.claims import REPO_ROOT

    return "sha256:" + hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()


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


# -- the gaming probe ------------------------------------------------------
#
# Measured against the pre-hardening module on 2026-08-03:
#     verdict=SUPPORTED  max_knowledge_tier=T4_REPRODUCIBLE  findings=[]
# A false load-bearing claim, a three-letter quote, an invented section number.


def test_the_gaming_probe_no_longer_passes():
    """A false claim, quoting "the", at a section that does not exist."""
    claim = Claim(
        claim_id="PROBE-1",
        statement="The contract permits an uncalibrated model judge to issue final verdicts.",
        citations=[
            cite_repo_file(
                source_id="PROBE",
                path=CONTRACT,
                quote="the",
                exact_location="§99.9 (does not exist)",
                applicability="EFAH-CONTRACT-001",
            )
        ],
        affected_requirement="REQ-PROBE",
    )
    result = validate_claim(claim)

    assert not result.supported
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert result.max_knowledge_tier is KnowledgeTier.T2_HYPOTHESIS
    assert result.findings, "a rejected claim must say why"
    assert any("characters" in f or "word" in f for f in result.findings)


def test_the_gaming_probe_with_a_substantive_quote_still_fails_on_its_location():
    """Lengthen the quote and the invented section is what stops it."""
    claim = Claim(
        claim_id="PROBE-2",
        statement="The contract permits an uncalibrated model judge to issue final verdicts.",
        citations=[
            cite_repo_file(
                source_id="PROBE",
                path=CONTRACT,
                quote=REAL_QUOTE,
                exact_location="§99.9 (does not exist)",
                applicability="EFAH-CONTRACT-001",
            )
        ],
        affected_requirement="REQ-PROBE",
    )
    result = validate_claim(claim)

    assert result.verdict is ClaimVerdict.UNSUPPORTED
    assert any("does not exist in" in f for f in result.findings)


def test_the_gaming_probe_with_a_real_location_still_fails_on_linkage():
    """Quote something real, from a real section, about something else."""
    claim = Claim(
        claim_id="PROBE-3",
        statement="The contract permits an uncalibrated model judge to issue final verdicts.",
        citations=[_citation()],
        affected_requirement="REQ-PROBE",
    )
    result = validate_claim(claim)

    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert any("content word" in f for f in result.findings)


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


def test_an_incidental_uncited_claim_is_unevidenced_not_supported():
    """The bypass that made ``load_bearing=False`` worth setting.

    An uncited incidental claim used to return SUPPORTED at T4 — a free ride
    into the tier system for anything an author was willing to call incidental.
    It is not an error to make an uncited incidental claim; it is an error to
    call one *supported*, because nothing was checked.
    """
    claim = Claim(claim_id="C-6", statement="this run took a while", citations=[], load_bearing=False)
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert result.max_knowledge_tier is KnowledgeTier.T2_HYPOTHESIS
    assert any("not evidence" in f for f in result.findings)


def test_an_incidental_claim_faces_the_same_evidence_rules_as_a_load_bearing_one():
    """``load_bearing`` records intent and buys nothing."""
    fabricated = Claim(
        claim_id="C-6b",
        statement="the contract requires second-agent review of every claim",
        citations=[_citation(quote=FABRICATED_QUOTE)],
        load_bearing=False,
    )
    assert validate_claim(fabricated).verdict is ClaimVerdict.UNSUPPORTED

    model_only = Claim(
        claim_id="C-6c",
        statement="every load-bearing claim must record its source",
        citations=[_citation(source_class=SourceClass.MODEL_ASSERTION)],
        load_bearing=False,
    )
    assert validate_claim(model_only).verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE


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
    claim = Claim(
        claim_id="C-9",
        statement="every load-bearing claim must record its source",
        citations=[_citation()],
    )
    body = validate_claim(claim).as_body()
    assert body["model_judge_in_verdict_path"] is False
    assert body["oracle_type"] == "exact_deterministic_execution_or_state"


def test_validation_is_a_compiled_object_with_an_envelope():
    claim = Claim(
        claim_id="C-10",
        statement="every load-bearing claim must record its source",
        citations=[_citation()],
    )
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


# -- 1. a quote must be big enough to be evidence --------------------------
#
# "the quote is present in the source" is only information if the quote is
# unlikely to be present by chance. Three letters are not.


@pytest.mark.parametrize("quote", ["the", "a", "MUST", "claim MUST record"])
def test_a_quote_too_small_to_be_evidence_is_malformed(quote):
    check = verify_citation(_citation(quote=quote))
    assert check.status is CitationStatus.MALFORMED
    assert "chance" in check.detail or "same reason" in check.detail


def test_the_floor_is_stated_as_a_number_and_a_real_clause_clears_it():
    from research.claims import _normalise

    assert len(_normalise(REAL_QUOTE)) >= MINIMUM_QUOTE_CHARACTERS
    assert verify_citation(_citation()).status is CitationStatus.VERIFIED


def test_a_short_quote_cannot_be_smuggled_past_the_floor_with_whitespace():
    """The floor is measured after normalisation, like the match itself."""
    check = verify_citation(_citation(quote="the" + " " * 60))
    assert check.status is CitationStatus.MALFORMED


# -- 1b. linkage: the deterministic floor, and its stated limits ------------


def test_a_real_quote_about_something_else_does_not_support_the_statement():
    claim = Claim(
        claim_id="L-1",
        statement="the scheduler leases a task for ninety seconds",
        citations=[_citation()],
        affected_requirement="REQ-LEASE",
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert any("content word" in f for f in result.findings)


def test_linkage_is_lexical_overlap_and_says_so():
    assert statement_quote_overlap("a claim must record its source", REAL_QUOTE) == {
        "claim",
        "must",
        "record",
    }
    # normative words are deliberately not stopwords: "MUST" and "MUST NOT" are
    # the words a contract claim usually turns on.
    assert "must" in content_tokens("a claim must record")
    assert statement_quote_overlap("the scheduler leases a task", REAL_QUOTE) == set()


def test_the_plural_fold_links_claim_and_claims():
    assert content_tokens("claims") == {"claim"}
    assert statement_quote_overlap("load-bearing claims are recorded", REAL_QUOTE) == {
        "load",
        "bearing",
        "claim",
    }


def test_the_linkage_floor_is_defeated_by_copying_one_word():
    """The residual, asserted rather than left implicit.

    Relevance is a semantic judgement and §7.3's gate declares
    ``model_judge_in_verdict_path: false``, so this module cannot make one. An
    author who writes the statement in the source's vocabulary passes the floor
    while claiming the opposite of what the source says. This test exists so the
    limit is visible in the suite rather than only in a docstring.
    """
    claim = Claim(
        claim_id="L-2",
        statement="No load-bearing claim need record anything at all.",
        citations=[_citation()],
        affected_requirement="REQ-RESIDUAL",
    )
    assert validate_claim(claim).verdict is ClaimVerdict.SUPPORTED


# -- 2. the cited location is checked, not echoed ---------------------------


def test_a_quote_at_the_wrong_section_does_not_verify():
    """The quote is really in the file — just not where the citation says."""
    check = verify_citation(_citation(exact_location="§7.4 Hypothesis-based research"))
    assert check.status is CitationStatus.UNSUPPORTED
    assert "not at" in check.detail


def test_a_section_that_does_not_exist_does_not_verify():
    check = verify_citation(_citation(exact_location="§99.9 (does not exist)"))
    assert check.status is CitationStatus.UNSUPPORTED
    assert "does not exist in" in check.detail


def test_a_location_the_grammar_cannot_read_is_malformed():
    check = verify_citation(_citation(exact_location="somewhere near the top, I think"))
    assert check.status is CitationStatus.MALFORMED
    assert LOCATION_GRAMMAR in check.detail


def test_a_line_range_locates_and_a_wrong_one_does_not():
    payload = b"alpha\nEvery load-bearing claim MUST record the source\nomega\n"
    hashed = "sha256:" + hashlib.sha256(payload).hexdigest()

    def reader(_pointer):
        return payload

    right = verify_citation(
        _citation(exact_location="lines 2-3", content_hash=hashed), reader=reader
    )
    assert right.status is CitationStatus.VERIFIED

    wrong = verify_citation(
        _citation(exact_location="lines 3-3", content_hash=hashed), reader=reader
    )
    assert wrong.status is CitationStatus.UNSUPPORTED

    past_eof = verify_citation(
        _citation(exact_location="lines 40-90", content_hash=hashed), reader=reader
    )
    assert past_eof.status is CitationStatus.UNSUPPORTED


def test_a_line_range_wide_enough_to_be_a_search_is_not_an_exact_location():
    check = verify_citation(_citation(exact_location=f"lines 1-{MAXIMUM_LOCATION_LINES + 1}"))
    assert check.status is CitationStatus.MALFORMED
    assert "not an exact supporting location" in check.detail


@pytest.mark.parametrize(
    "written,expected",
    [
        ("§7.3 Source assurance", ("7.3", None, None)),
        ("section 7.3", ("7.3", None, None)),
        ("Section 15.5 Knowledge tiers", ("15.5", None, None)),
        ("lines 10-20", (None, 10, 20)),
        ("line 10", (None, 10, 10)),
        ("L10-L20", (None, 10, 20)),
        ("L7", (None, 7, 7)),
    ],
)
def test_the_location_grammar_is_small_and_closed(written, expected):
    location = parse_location(written)
    assert location is not None
    assert (location.section_id, location.first_line, location.last_line) == expected


@pytest.mark.parametrize("written", ["somewhere", "the second paragraph", "", "§"])
def test_locations_outside_the_grammar_do_not_parse(written):
    assert parse_location(written) is None


def test_a_section_ends_at_the_next_heading_of_the_same_or_higher_level():
    text = "# 7. Research\n\nintro\n\n## 7.3 Source\n\nbody\n\n## 7.4 Next\n\nother\n"
    body = locate(text, parse_location("§7.3"))
    assert "body" in body
    assert "other" not in body
    assert "intro" not in body


def test_a_source_without_headings_cannot_be_cited_by_section():
    """The honest answer is "cite lines", not "search the whole file"."""
    assert locate("plain text with no headings at all\n", parse_location("§7.3")) is None


# -- 3 and 4. one bad citation is not laundered by a good one ---------------


def test_a_verified_citation_does_not_carry_an_empty_malformed_one():
    """§7.3's missing fields were recorded and then ignored.

    One VERIFIED citation plus one entirely empty citation returned SUPPORTED,
    so the structural findings were decoration: an author could attach a real
    citation and any number of blank ones.
    """
    empty = Citation(
        source_id="",
        source_pointer="",
        source_class=SourceClass.SOURCE_CODE,
        retrieved_at="",
        exact_location="",
        quote="",
        support_kind=SupportKind.DIRECT,
        applicability="",
        content_hash="",
        retrieval_provenance="",
    )
    claim = Claim(
        claim_id="M-1",
        statement="every load-bearing claim must record its source",
        citations=[_citation(), empty],
        affected_requirement="REQ-CITATION",
    )
    result = validate_claim(claim)
    assert result.verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert not result.supported
    assert any("did not" in f for f in result.findings)


def test_a_verified_citation_does_not_carry_an_unresolvable_one():
    claim = Claim(
        claim_id="M-2",
        statement="every load-bearing claim must record its source",
        citations=[_citation(), _citation(source_id="GONE", source_pointer="does/not/exist.md")],
        affected_requirement="REQ-CITATION",
    )
    assert validate_claim(claim).verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE


def test_a_fabricated_citation_still_outranks_a_malformed_one_in_the_verdict():
    """The interesting failure must not hide behind the boring one."""
    empty = Citation(
        source_id="",
        source_pointer="",
        source_class=SourceClass.SOURCE_CODE,
        retrieved_at="",
        exact_location="",
        quote="",
        support_kind=SupportKind.DIRECT,
        applicability="",
        content_hash="",
        retrieval_provenance="",
    )
    claim = Claim(
        claim_id="M-3",
        statement="the contract requires second-agent review of every claim",
        citations=[_citation(quote=FABRICATED_QUOTE), empty],
    )
    assert validate_claim(claim).verdict is ClaimVerdict.UNSUPPORTED


# -- 5. the hash is over raw bytes ------------------------------------------


def test_the_content_hash_is_over_raw_bytes_not_a_decoded_string():
    """``read_text()`` translates newlines; the hash must not.

    Hashing a decoded string makes the recorded hash depend on the decoder — CRLF
    folds to LF, undecodable bytes fold to U+FFFD — so two different files can
    hash the same and a changed source reads as unchanged.
    """
    payload = b"alpha\r\nEvery load-bearing claim MUST record the source\r\nomega\r\n"
    raw = "sha256:" + hashlib.sha256(payload).hexdigest()
    decoded = "sha256:" + hashlib.sha256(payload.decode().replace("\r\n", "\n").encode()).hexdigest()
    assert raw != decoded

    check = verify_citation(
        _citation(exact_location="lines 1-3", content_hash=raw), reader=lambda _p: payload
    )
    assert check.status is CitationStatus.VERIFIED
    assert check.observed_hash == raw


def test_cite_repo_file_records_the_hash_of_the_bytes_on_disk():
    citation = cite_repo_file(
        source_id="C7.3",
        path=CONTRACT,
        quote=REAL_QUOTE,
        exact_location="§7.3",
        applicability="this contract",
    )
    from research.claims import REPO_ROOT

    assert citation.content_hash == "sha256:" + hashlib.sha256(
        (REPO_ROOT / CONTRACT).read_bytes()
    ).hexdigest()


def test_a_binary_source_is_unresolvable_not_stale():
    """A ``.pyc`` used to decode to mojibake and be reported as *changed*.

    That was a claim about the source's history that nobody had checked. The
    pointer resolves; a byte-exact quote check is simply not defined over these
    bytes, which is what UNRESOLVABLE means.
    """
    check = verify_citation(_citation(), reader=lambda _p: b"\x00\x01\xff\xfe python bytecode")
    assert check.status is CitationStatus.UNRESOLVABLE
    assert "not UTF-8" in check.detail


def test_decoding_names_its_encoding_rather_than_following_the_host_locale():
    with pytest.raises(SourceUnreadable):
        decode_source("x.pyc", b"\xff\xfe\x00")
    assert decode_source("x.md", "héllo".encode()) == "héllo"


def test_a_reader_that_returns_text_is_refused():
    """The hash is over what was read; text has already been decoded by someone."""
    with pytest.raises(TypeError):
        verify_citation(_citation(), reader=lambda _p: "text, not bytes")


# -- 6. a pointer names evidence inside this repository ---------------------


def test_an_absolute_pointer_is_refused():
    check = verify_citation(_citation(source_pointer="/etc/hostname"))
    assert check.status is CitationStatus.UNRESOLVABLE
    assert "absolute" in check.detail


def test_a_traversal_out_of_the_repository_is_refused():
    check = verify_citation(_citation(source_pointer="../../../etc/passwd"))
    assert check.status is CitationStatus.UNRESOLVABLE
    assert "outside the repository" in check.detail


def test_reading_outside_the_repository_raises_at_the_reader():
    with pytest.raises(SourceUnreadable):
        read_source_bytes("/etc/hostname")
    with pytest.raises(SourceUnreadable):
        read_source_bytes("../../../etc/passwd")
    assert read_source_bytes(CONTRACT).startswith(b"# FINAL BUILD CONTRACT")


# -- the no-judge property this module's rank depends on --------------------


def test_the_module_still_proves_no_model_judge_in_its_closure():
    """§17.4 wants structural proof, and every check above must keep it true."""
    from oracles.no_judge import prove_no_judge

    proof = prove_no_judge("research.claims")
    assert proof.holds, proof.violations
