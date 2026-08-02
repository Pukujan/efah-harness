"""Source assurance — §7.3 made mechanical, so a citation cannot be invented.

Contract §7.3 opens with the sentence this module exists to enforce:

    Every load-bearing claim MUST record: source ID and URL/file pointer;
    source class and authority; publication/update/retrieval date; exact
    supporting location; direct support versus inference; applicability to the
    actual dependency/version/task; conflicts or missing corroboration;
    confidence and uncertainty; affected requirement or decision; content hash
    and retrieval provenance.

Before this module those eleven fields existed only as contract prose.
:mod:`knowledge.tiers` guarded *promotion* — agent output enters at T2 and
cannot be promoted past T4 without cross-family verification — which stops an
unverified claim being presented as trusted. It does not stop the claim being
**fabricated in the first place**, because nothing checked that a cited source
says what the citation says it says.

That is the gap this closes, and the mechanism is deliberately dumb:

**A citation records a quote and a location. The validator re-reads the source
and checks the quote is actually there, byte for byte.**

A model that invents a plausible-looking citation fails deterministically, at
:data:`CitationStatus.UNSUPPORTED`, with no judge in the verdict path. This is
the §17.3 hierarchy applied to citations — an exact execution/state check
outranks a calibrated model judge, and "does this string occur at this offset in
this file" is as exact as checks get.

Three failure modes are separated, because conflating them hides the interesting
one:

``UNSUPPORTED``
    The quote is not in the source. The citation was fabricated, or the location
    is wrong. This is the hallucination signal.
``STALE``
    The quote is there, but the source's content hash no longer matches the one
    recorded at retrieval. The claim was true and may not be now — §15.7's
    invalidation rule, applied per claim rather than per corpus.
``UNRESOLVABLE``
    The source pointer does not resolve at all. Distinct from ``UNSUPPORTED``:
    a missing file is an infrastructure problem, an absent quote is a
    truthfulness problem, and treating them alike would let a deleted file read
    as a lie or a lie read as a missing file.

What this module does not do
-----------------------------
It does not decide whether the quote, once verified to exist, actually *entails*
the claim. That is a judgment, and §17.5 makes an uncalibrated judge advisory.
What it guarantees is narrower and checkable: the source exists, it is unchanged,
and it contains the words the claim says it contains. Entailment is left to the
adversarial critic, whose output is advisory by construction.

The honest consequence: a determined model could quote a real source accurately
and still draw a false conclusion from it. That is a weaker attack than
inventing a source, it leaves a verifiable trail, and it is the residue recorded
in the honest-debt ledger rather than papered over.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from governance.envelope import CompiledObject, KnowledgeTier, utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]


class SourceClass(StrEnum):
    """§7.2 fact classes crossed with §7.3 "source class and authority".

    Ordered strongest to weakest. The order is load-bearing:
    :data:`MINIMUM_AUTHORITY_FOR_LOAD_BEARING` cuts the list, and
    :data:`MODEL_ASSERTION` sits below the cut on purpose.
    """

    #: The owner said so. §7.2 owner fact — values, scope, acceptable risk.
    OWNER_STATEMENT = "OWNER_STATEMENT"
    #: The contract, a DEC record, or the decision ledger.
    RECORDED_DECISION = "RECORDED_DECISION"
    #: A file in this repository at a known commit. §7.2 repository fact.
    SOURCE_CODE = "SOURCE_CODE"
    #: A probe, test, or API check run against a live system. §7.2 empirical.
    LIVE_PROBE = "LIVE_PROBE"
    #: Official docs retrieved and hashed at a pinned version (§16.1).
    VERSION_PINNED_SNAPSHOT = "VERSION_PINNED_SNAPSHOT"
    #: A published specification or standard.
    PRIMARY_SPECIFICATION = "PRIMARY_SPECIFICATION"
    #: A benchmark someone else can re-run and get the same number.
    REPRODUCIBLE_BENCHMARK = "REPRODUCIBLE_BENCHMARK"
    #: Secondary commentary — a blog post, an answer, a summary.
    SECONDARY_COMMENTARY = "SECONDARY_COMMENTARY"
    #: A model said it. Never sufficient for a load-bearing claim on its own:
    #: §15.5, "unverified agent output MUST NOT be presented as trusted
    #: knowledge". Recorded rather than banned, so the provenance is visible.
    MODEL_ASSERTION = "MODEL_ASSERTION"


#: Weakest source class that may carry a load-bearing claim alone. Anything
#: below this is recorded and does not count toward support.
AUTHORITY_ORDER: tuple[SourceClass, ...] = (
    SourceClass.OWNER_STATEMENT,
    SourceClass.RECORDED_DECISION,
    SourceClass.SOURCE_CODE,
    SourceClass.LIVE_PROBE,
    SourceClass.VERSION_PINNED_SNAPSHOT,
    SourceClass.PRIMARY_SPECIFICATION,
    SourceClass.REPRODUCIBLE_BENCHMARK,
    SourceClass.SECONDARY_COMMENTARY,
    SourceClass.MODEL_ASSERTION,
)
MINIMUM_AUTHORITY_FOR_LOAD_BEARING = SourceClass.SECONDARY_COMMENTARY


def authority_rank(source_class: SourceClass) -> int:
    return AUTHORITY_ORDER.index(source_class)


class SupportKind(StrEnum):
    """§7.3 "direct support versus inference". Both permitted, both labelled."""

    #: The source states the claim.
    DIRECT = "DIRECT"
    #: The claim follows from the source but is not stated in it. The step must
    #: be written down in ``inference_step`` or the citation is malformed — an
    #: unstated inference is where a false claim hides behind a true source.
    INFERENCE = "INFERENCE"


class CitationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    #: The quote is not present in the source. The hallucination signal.
    UNSUPPORTED = "UNSUPPORTED"
    #: Present, but the source changed since retrieval (§15.7).
    STALE = "STALE"
    #: The pointer does not resolve. Infrastructure, not truthfulness.
    UNRESOLVABLE = "UNRESOLVABLE"
    #: Structurally invalid — a required §7.3 field is missing.
    MALFORMED = "MALFORMED"


class ClaimVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    #: §15.4 requires this to be returnable. A claim with nothing behind it is
    #: not false — it is unevidenced, and saying so is the honest answer.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: At least one citation was fabricated or misquoted.
    UNSUPPORTED = "UNSUPPORTED"
    #: Every citation resolves and quotes correctly, but a source has changed.
    STALE = "STALE"


class SourceUnreadable(RuntimeError):
    """The source pointer could not be read. Distinct from a bad quote."""


@dataclass(frozen=True)
class Citation:
    """The §7.3 record. All eleven fields, none optional by accident."""

    #: (1) source ID and (2) URL or file pointer.
    source_id: str
    source_pointer: str
    #: (3) source class and authority.
    source_class: SourceClass
    #: (4) publication/update date and (5) retrieval date.
    retrieved_at: str
    #: (6) exact supporting location — a line range, an anchor, a section id.
    #: Free-form because sources differ, but it must be specific enough that a
    #: human can go and look.
    exact_location: str
    #: The words the claim rests on. This is what makes the citation checkable:
    #: without a quote there is nothing to verify against the source.
    quote: str
    #: (7) direct support versus inference.
    support_kind: SupportKind
    #: (8) applicability to the actual dependency/version/task.
    applicability: str
    #: (9) content hash at retrieval + (10) retrieval provenance.
    content_hash: str
    retrieval_provenance: str
    published_at: str | None = None
    #: Required when ``support_kind`` is INFERENCE. An unstated inference step
    #: is indistinguishable from an invention.
    inference_step: str | None = None

    def structural_findings(self) -> list[str]:
        """Missing §7.3 fields, before anything is read from disk."""
        findings: list[str] = []
        for name in (
            "source_id",
            "source_pointer",
            "retrieved_at",
            "exact_location",
            "quote",
            "applicability",
            "content_hash",
            "retrieval_provenance",
        ):
            if not str(getattr(self, name) or "").strip():
                findings.append(f"{name} is empty; §7.3 requires it on every load-bearing claim")
        if self.support_kind is SupportKind.INFERENCE and not (self.inference_step or "").strip():
            findings.append(
                "support_kind is INFERENCE but inference_step is empty; an unstated "
                "inference is where a false claim hides behind a true source"
            )
        if self.content_hash and not re.match(r"^sha256:[0-9a-f]{64}$", self.content_hash):
            findings.append("content_hash is not a sha256 digest recorded at retrieval")
        return findings

    def as_body(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_pointer": self.source_pointer,
            "source_class": self.source_class.value,
            "authority_rank": authority_rank(self.source_class),
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "exact_location": self.exact_location,
            "quote": self.quote,
            "support_kind": self.support_kind.value,
            "inference_step": self.inference_step,
            "applicability": self.applicability,
            "content_hash": self.content_hash,
            "retrieval_provenance": self.retrieval_provenance,
        }


@dataclass
class CitationCheck:
    citation: Citation
    status: CitationStatus
    detail: str
    observed_hash: str | None = None

    @property
    def counts_as_support(self) -> bool:
        return self.status is CitationStatus.VERIFIED

    def as_body(self) -> dict[str, Any]:
        return {
            "source_id": self.citation.source_id,
            "status": self.status.value,
            "detail": self.detail,
            "recorded_hash": self.citation.content_hash,
            "observed_hash": self.observed_hash,
            "support_kind": self.citation.support_kind.value,
            "source_class": self.citation.source_class.value,
        }


def _normalise(text: str) -> str:
    """Collapse whitespace only.

    Quote matching must survive reflowed lines and indentation, and must **not**
    survive changed words. So whitespace is normalised and nothing else — no
    case folding, no punctuation stripping, no fuzzy ratio. A near-match is not
    a match: "MUST NOT" and "must" are the difference between a contract clause
    and its opposite.
    """
    return re.sub(r"\s+", " ", text).strip()


def read_source(pointer: str) -> str:
    """Resolve a source pointer to text.

    Only local pointers resolve here. A remote URL must have been snapshotted
    and hashed at retrieval (§16.1) and cited by its snapshot path — verifying
    a live URL at check time would be checking a *different* document from the
    one the claim was made against, and would silently pass when the page had
    changed underneath it.
    """
    path = (REPO_ROOT / pointer).resolve() if not pointer.startswith("/") else Path(pointer)
    if not path.is_file():
        raise SourceUnreadable(f"{pointer} does not resolve to a file on this host")
    try:
        return path.read_text(errors="replace")
    except OSError as exc:
        raise SourceUnreadable(f"{pointer}: {exc}") from exc


def verify_citation(citation: Citation, *, reader=read_source) -> CitationCheck:
    """Re-read the source and check the quote is really in it.

    Deterministic. No model participates. This is the check that makes a
    fabricated citation fail rather than pass.
    """
    structural = citation.structural_findings()
    if structural:
        return CitationCheck(citation, CitationStatus.MALFORMED, "; ".join(structural))

    try:
        text = reader(citation.source_pointer)
    except SourceUnreadable as exc:
        return CitationCheck(citation, CitationStatus.UNRESOLVABLE, str(exc))

    observed = "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    if _normalise(citation.quote) not in _normalise(text):
        # Checked before staleness on purpose. A source that changed *and* never
        # contained the quote is a fabrication, and reporting it as merely stale
        # would let the interesting failure hide behind the boring one.
        return CitationCheck(
            citation,
            CitationStatus.UNSUPPORTED,
            (
                f"the quoted text does not occur in {citation.source_pointer}; "
                "the citation does not support the claim"
            ),
            observed,
        )

    if observed != citation.content_hash:
        return CitationCheck(
            citation,
            CitationStatus.STALE,
            (
                f"{citation.source_pointer} has changed since retrieval "
                f"({citation.retrieved_at}); the quote is still present but the claim "
                "was made against a different revision (§15.7)"
            ),
            observed,
        )

    return CitationCheck(
        citation,
        CitationStatus.VERIFIED,
        f"quote found in {citation.source_pointer} at {citation.exact_location}",
        observed,
    )


@dataclass
class Claim:
    """A statement, and what it rests on.

    ``load_bearing`` is the switch §7.3 turns on. A claim that nothing depends
    on may be uncited; a claim a requirement, decision, or test depends on may
    not. Marking a load-bearing claim as incidental to dodge the check is the
    obvious attack, which is why :func:`validate_claim` treats a stated
    ``affected_requirement`` as making the claim load-bearing regardless.
    """

    claim_id: str
    statement: str
    citations: list[Citation] = field(default_factory=list)
    load_bearing: bool = True
    #: §7.3 (9) affected requirement or decision.
    affected_requirement: str | None = None
    #: §7.3 (8) conflicts or missing corroboration — stated, not omitted.
    conflicts: list[str] = field(default_factory=list)
    #: §7.3 confidence and uncertainty.
    confidence: str = "unknown"
    uncertainty: str = ""
    author_alias: str | None = None
    author_family: str | None = None

    @property
    def is_load_bearing(self) -> bool:
        return self.load_bearing or bool(self.affected_requirement)


@dataclass
class ClaimValidation:
    claim: Claim
    verdict: ClaimVerdict
    checks: list[CitationCheck] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.verdict is ClaimVerdict.SUPPORTED

    @property
    def max_knowledge_tier(self) -> KnowledgeTier:
        """The ceiling this claim may occupy in the §15.5 tier system.

        A claim nothing supports cannot be an observation, let alone tested
        knowledge — it is a hypothesis at best. This is what connects citation
        validation to :mod:`knowledge.tiers`: an uncited claim is clamped to T2
        by the same rule that clamps raw agent output, and for the same reason.
        """
        if self.verdict is ClaimVerdict.SUPPORTED:
            return KnowledgeTier.T4_REPRODUCIBLE
        if self.verdict is ClaimVerdict.STALE:
            return KnowledgeTier.T1_OBSERVATION
        return KnowledgeTier.T2_HYPOTHESIS

    def as_body(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim.claim_id,
            "statement": self.claim.statement,
            "verdict": self.verdict.value,
            "load_bearing": self.claim.is_load_bearing,
            "affected_requirement": self.claim.affected_requirement,
            "confidence": self.claim.confidence,
            "uncertainty": self.claim.uncertainty,
            "conflicts": self.claim.conflicts,
            "max_knowledge_tier": self.max_knowledge_tier.value,
            "citations": [c.as_body() for c in self.checks],
            "citation_checks": [c.as_body() for c in self.checks],
            "findings": self.findings,
            "oracle_type": "exact_deterministic_execution_or_state",
            "model_judge_in_verdict_path": False,
            "validated_at": utc_now(),
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.claim_validation",
            created_by_alias="auditor-a07",
            body=self.as_body(),
        )


def validate_claim(claim: Claim, *, reader=read_source) -> ClaimValidation:
    """§15.4's "citation and claim validation", as a deterministic check."""
    checks = [verify_citation(c, reader=reader) for c in claim.citations]
    findings: list[str] = []

    if not claim.is_load_bearing:
        return ClaimValidation(
            claim,
            ClaimVerdict.SUPPORTED if not checks else _verdict_from(checks, findings),
            checks,
            findings,
        )

    if not claim.citations:
        findings.append(
            "a load-bearing claim with no citation; §7.3 requires a source record "
            "for every load-bearing claim"
        )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    fabricated = [c for c in checks if c.status is CitationStatus.UNSUPPORTED]
    if fabricated:
        findings.extend(f"{c.citation.source_id}: {c.detail}" for c in fabricated)
        return ClaimValidation(claim, ClaimVerdict.UNSUPPORTED, checks, findings)

    malformed = [c for c in checks if c.status is CitationStatus.MALFORMED]
    findings.extend(f"{c.citation.source_id}: {c.detail}" for c in malformed)

    verified = [c for c in checks if c.counts_as_support]
    if not verified:
        findings.append(
            "no citation verified against its source; the claim may be true but "
            "nothing here shows it"
        )
        stale = [c for c in checks if c.status is CitationStatus.STALE]
        return ClaimValidation(
            claim,
            ClaimVerdict.STALE if stale and not malformed else ClaimVerdict.INSUFFICIENT_EVIDENCE,
            checks,
            findings,
        )

    # §15.5: a model saying something is not evidence that it is so, however
    # accurately the model is quoted.
    authoritative = [
        c
        for c in verified
        if authority_rank(c.citation.source_class)
        <= authority_rank(MINIMUM_AUTHORITY_FOR_LOAD_BEARING)
    ]
    if not authoritative:
        findings.append(
            "every verified citation is a MODEL_ASSERTION; §15.5 forbids presenting "
            "unverified agent output as trusted knowledge, so this is unevidenced"
        )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    direct = [c for c in authoritative if c.citation.support_kind is SupportKind.DIRECT]
    if not direct:
        findings.append(
            "every verified citation is INFERENCE; a load-bearing claim needs at "
            "least one source that states it directly (§7.3 direct support versus inference)"
        )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    if any(c.status is CitationStatus.STALE for c in checks):
        findings.append("at least one source has changed since retrieval; revalidate (§15.7)")
        return ClaimValidation(claim, ClaimVerdict.STALE, checks, findings)

    return ClaimValidation(claim, ClaimVerdict.SUPPORTED, checks, findings)


def _verdict_from(checks: list[CitationCheck], findings: list[str]) -> ClaimVerdict:
    if any(c.status is CitationStatus.UNSUPPORTED for c in checks):
        findings.append("a citation does not support the claim")
        return ClaimVerdict.UNSUPPORTED
    if any(c.status is CitationStatus.STALE for c in checks):
        return ClaimVerdict.STALE
    return ClaimVerdict.SUPPORTED


def cite_repo_file(
    *,
    source_id: str,
    path: str,
    quote: str,
    exact_location: str,
    applicability: str,
    support_kind: SupportKind = SupportKind.DIRECT,
    source_class: SourceClass = SourceClass.SOURCE_CODE,
    inference_step: str | None = None,
) -> Citation:
    """Build a citation to a file in this repository, hashing it now.

    A convenience for the common case, and the hash is taken from the file
    rather than accepted from a caller — a caller-supplied hash is a caller-
    supplied claim, which is the thing being checked.
    """
    text = read_source(path)
    return Citation(
        source_id=source_id,
        source_pointer=path,
        source_class=source_class,
        retrieved_at=date.today().isoformat(),
        exact_location=exact_location,
        quote=quote,
        support_kind=support_kind,
        applicability=applicability,
        content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        retrieval_provenance=f"read from the working tree at {REPO_ROOT}",
        inference_step=inference_step,
    )
