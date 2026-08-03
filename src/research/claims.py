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
and checks that quote is really there, byte for byte, at that location.**

Every check below is an exact string, byte, or arithmetic comparison. No model
participates in any verdict, which is what lets §17.3 rank this above a
calibrated judge: "does this string occur inside this span of this file" is as
exact as checks get.

Three failure modes are separated, because conflating them hides the interesting
one:

``UNSUPPORTED``
    The quote is not in the source, or not at the location the citation names.
    The citation was fabricated, or the location was invented. This is the
    hallucination signal.
``STALE``
    The quote is there, but the source's content hash no longer matches the one
    recorded at retrieval. The claim was true and may not be now — §15.7's
    invalidation rule, applied per claim rather than per corpus.
``UNRESOLVABLE``
    The source pointer does not resolve, points outside the repository, or names
    bytes that are not text. Distinct from ``UNSUPPORTED``: a missing file is an
    infrastructure problem, an absent quote is a truthfulness problem, and
    treating them alike would let a deleted file read as a lie or a lie read as
    a missing file.
``MALFORMED``
    The citation record is structurally invalid before anything is read — a
    missing §7.3 field, a quote too short to be evidence, a location that names
    nothing checkable. A malformed citation never counts as support and never
    leaves a claim ``SUPPORTED``.

Why a quote has a minimum size (:data:`MINIMUM_QUOTE_CHARACTERS`)
-----------------------------------------------------------------
The quote check is only evidence if finding the quote is *improbable by
chance*. ``quote="the"`` occurs in essentially every English document, so
"the quote is present in the source" carries no information about the claim; it
verifies against any file at any location. A floor on quote size is therefore
not decoration on the check, it is the condition under which the check means
anything, and it is mechanical: a character count and a token count, computed
after whitespace normalisation, with no judgement involved.

The floor is stated as a constant rather than a heuristic so it can be argued
with. It is deliberately low — a short contract clause fragment must still be
citable — and it is a *necessary*, never a sufficient, condition.

Why linkage is only a lexical overlap floor
--------------------------------------------
A quote can be long, real, correctly located, and about something else entirely.
The honest fix would be to ask whether the quote bears on the statement, and
that is a semantic judgement. §7.3's gate declares
``model_judge_in_verdict_path: false``, so this module may not make one: adding
a model here would forfeit the property that makes the whole check outrank a
judge in the §17.3 hierarchy.

What is left is the strongest deterministic approximation available without a
model: :func:`statement_quote_overlap` requires the statement and the quote to
share at least :data:`MINIMUM_STATEMENT_QUOTE_OVERLAP` content words (case
folded, stopwords dropped, one crude plural fold). This catches the case where
a real quote is bolted onto an unrelated statement — the realistic gaming move,
because a fabricator finds it easier to quote something true than to quote
something true *and* on topic.

**Stated plainly, because it is the residual and not a solved problem:** a
determined author defeats the linkage floor by copying one word of the statement
into the quoted range, or by wording the statement in the source's vocabulary.
Lexical overlap is not relevance. This module does not, and with the no-judge
constraint cannot, decide whether a verified quote *entails* the claim. It also
false-rejects in the other direction: a genuinely supporting quote that shares
no vocabulary with the statement is reported as unevidenced, which errs toward
"not proven" and is the direction an evidence check should err in.

What this module does not do
-----------------------------
It does not decide entailment (above). It does not check that the *rest* of the
source, outside the quoted span, fails to contradict the claim. It does not stop
an author citing a real, correctly located, on-topic quote and drawing a false
conclusion from it. That is a weaker attack than inventing a source, it leaves a
verifiable trail, and it is the residue recorded in the honest-debt ledger
rather than papered over.
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

#: A quote shorter than this is not evidence: it occurs by chance. Both floors
#: apply, and both are measured after whitespace normalisation. See the module
#: docstring for the argument; these numbers are the argument's parameters and
#: are meant to be argued with, not treated as magic.
MINIMUM_QUOTE_CHARACTERS = 24
MINIMUM_QUOTE_TOKENS = 4

#: Content words a statement and its quote must share. One is a floor, not a
#: relevance test — see "Why linkage is only a lexical overlap floor".
MINIMUM_STATEMENT_QUOTE_OVERLAP = 1

#: A cited line range wider than this is not an "exact supporting location"
#: (§7.3). Enforced on line ranges only, because a range's width is knowable
#: from the citation alone. Section references are exempt: their width is the
#: document's own structure, not the author's choice.
MAXIMUM_LOCATION_LINES = 120


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
    #: The quote is not present in the source, or not at the cited location.
    #: The hallucination signal.
    UNSUPPORTED = "UNSUPPORTED"
    #: Present, but the source changed since retrieval (§15.7).
    STALE = "STALE"
    #: The pointer does not resolve. Infrastructure, not truthfulness.
    UNRESOLVABLE = "UNRESOLVABLE"
    #: Structurally invalid — a required §7.3 field is missing, the quote is too
    #: short to be evidence, or the location names nothing checkable.
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


# -- location grammar -------------------------------------------------------
#
# §7.3 requires an "exact supporting location". Free-form prose cannot be
# checked, and an unchecked location is worse than none: the pre-hardening
# version of this module echoed the author's location string back inside the
# VERIFIED detail as though it had confirmed it. So the grammar is closed and
# small — if a location does not parse, the citation is MALFORMED, and the
# author is told which forms exist.

_SECTION_LOCATION = re.compile(r"^\s*(?:§|sec(?:tion)?\.?\s+)\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_LINE_LOCATION = re.compile(
    # the separator class covers hyphen, en dash, em dash, colon, and "to"
    r"^\s*(?:lines?\s+|L)(\d+)(?:\s*(?:[-\u2013\u2014:]|to\s+)\s*L?(\d+))?\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^(#{1,6})\s+§?\s*([0-9]+(?:\.[0-9]+)*)(?![0-9.])")

#: The forms a location may take, quoted back to the author on failure.
LOCATION_GRAMMAR = "'§7.3', 'section 7.3', 'lines 10-20', 'line 10', or 'L10-L20'"


@dataclass(frozen=True)
class SourceLocation:
    """A parsed :attr:`Citation.exact_location`: a section id or a line range."""

    section_id: str | None = None
    first_line: int | None = None
    last_line: int | None = None

    @property
    def line_count(self) -> int | None:
        if self.first_line is None or self.last_line is None:
            return None
        return self.last_line - self.first_line + 1


def parse_location(exact_location: str) -> SourceLocation | None:
    """Parse a location string, or return ``None`` if it names nothing checkable.

    Descriptive text after the location is ignored — ``"§7.3 Source assurance"``
    parses as section ``7.3`` — so a human-readable citation stays legal while
    the machine-checkable part stays mandatory.
    """
    text = str(exact_location or "")
    section = _SECTION_LOCATION.match(text)
    if section:
        return SourceLocation(section_id=section.group(1))
    lines = _LINE_LOCATION.match(text)
    if lines:
        first = int(lines.group(1))
        last = int(lines.group(2)) if lines.group(2) else first
        return SourceLocation(first_line=first, last_line=last)
    return None


def locate(text: str, location: SourceLocation) -> str | None:
    """Return the text the location selects, or ``None`` if it selects nothing.

    ``None`` means the location does not exist in this source — a section id
    with no heading, a line range past the end of the file, a reversed range.
    That is a citation failure, not an infrastructure failure: the author named
    a place that is not there.
    """
    lines = text.splitlines()
    if location.section_id is not None:
        return _section_body(lines, location.section_id)

    first, last = location.first_line, location.last_line
    if first is None or last is None or first < 1 or last < first or last > len(lines):
        return None
    return "\n".join(lines[first - 1 : last])


def _section_body(lines: list[str], section_id: str) -> str | None:
    """The markdown section headed *section_id*, up to the next same-or-higher heading.

    Markdown headings are the only section structure this understands. A source
    without them cannot be cited by section — it must be cited by line range,
    which is the honest answer rather than searching the whole file and calling
    that a location.
    """
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and heading.group(2) == section_id:
            start = index
            level = len(heading.group(1))
            break
    if start is None:
        return None

    end = len(lines)
    for index in range(start + 1, len(lines)):
        heading = _HEADING.match(lines[index])
        if heading and len(heading.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


# -- quote substantiveness and statement linkage ----------------------------

_WORD = re.compile(r"[a-z0-9]+")

#: Words carrying no linkage signal. Small on purpose: every word dropped here
#: is a word an author cannot use to connect a statement to its source, and
#: normative words ("must", "shall", "never") are deliberately *not* dropped.
_STOPWORDS = frozenset(
    {
        "the", "and", "that", "this", "these", "those", "for", "from", "with",
        "not", "but", "are", "was", "were", "been", "being", "has", "have",
        "had", "its", "any", "all", "can", "will", "would", "should", "which",
        "when", "then", "than", "there", "their", "them", "they", "into",
        "onto", "over", "under", "such", "only", "also", "each", "other",
        "some", "more", "most", "very", "upon", "about", "because", "while",
        "does", "did", "who", "whom", "whose", "how", "why", "what", "where",
    }
)


def content_tokens(text: str) -> set[str]:
    """Lower-cased content words, stopwords dropped, crude plural fold applied.

    The plural fold (a single trailing ``s`` removed from tokens longer than
    three characters) is deliberately crude rather than a stemmer: it is four
    lines of arithmetic on strings, it is the same on every host and every run,
    and it costs no dependency. It exists so "claim" and "claims" link.
    """
    tokens: set[str] = set()
    for raw in _WORD.findall(text.lower()):
        if len(raw) < 3 or raw in _STOPWORDS:
            continue
        folded = raw[:-1] if len(raw) > 3 and raw.endswith("s") else raw
        if folded in _STOPWORDS:
            continue
        tokens.add(folded)
    return tokens


def statement_quote_overlap(statement: str, quote: str) -> set[str]:
    """Content words shared by a statement and a quote.

    The deterministic stand-in for relevance. Its limits are stated in the
    module docstring and are real: this is a floor, not a relevance judgement.
    """
    return content_tokens(statement) & content_tokens(quote)


@dataclass(frozen=True)
class Citation:
    """The §7.3 record. All eleven fields, none optional by accident."""

    #: (1) source ID and (2) URL or file pointer. The pointer is a path relative
    #: to the repository root; absolute paths and ``..`` escapes are refused by
    #: :func:`read_source_bytes`.
    source_id: str
    source_pointer: str
    #: (3) source class and authority.
    source_class: SourceClass
    #: (4) publication/update date and (5) retrieval date.
    retrieved_at: str
    #: (6) exact supporting location — a section id or a line range, in one of
    #: the forms in :data:`LOCATION_GRAMMAR`. It is *checked*, not recorded: the
    #: quote must occur inside the span this selects.
    exact_location: str
    #: The words the claim rests on. This is what makes the citation checkable:
    #: without a quote there is nothing to verify against the source. Subject to
    #: :data:`MINIMUM_QUOTE_CHARACTERS` and :data:`MINIMUM_QUOTE_TOKENS`.
    quote: str
    #: (7) direct support versus inference.
    support_kind: SupportKind
    #: (8) applicability to the actual dependency/version/task.
    applicability: str
    #: (9) content hash at retrieval + (10) retrieval provenance. The hash is
    #: over the source's raw bytes.
    content_hash: str
    retrieval_provenance: str
    published_at: str | None = None
    #: Required when ``support_kind`` is INFERENCE. An unstated inference step
    #: is indistinguishable from an invention.
    inference_step: str | None = None

    def structural_findings(self) -> list[str]:
        """Everything checkable from the record alone, before anything is read."""
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
        findings.extend(self._quote_findings())
        findings.extend(self._location_findings())
        return findings

    def _quote_findings(self) -> list[str]:
        quote = _normalise(self.quote or "")
        if not quote:
            return []
        findings: list[str] = []
        if len(quote) < MINIMUM_QUOTE_CHARACTERS:
            findings.append(
                f"quote is {len(quote)} characters; §7.3 evidence requires at least "
                f"{MINIMUM_QUOTE_CHARACTERS}. A quote this short occurs by chance, so "
                "finding it in the source says nothing about the claim"
            )
        tokens = len(quote.split())
        if tokens < MINIMUM_QUOTE_TOKENS:
            findings.append(
                f"quote is {tokens} word(s); §7.3 evidence requires at least "
                f"{MINIMUM_QUOTE_TOKENS}, for the same reason"
            )
        return findings

    def _location_findings(self) -> list[str]:
        raw = str(self.exact_location or "").strip()
        if not raw:
            return []
        location = parse_location(raw)
        if location is None:
            return [
                f"exact_location {raw!r} names nothing a check can resolve; §7.3 requires an "
                f"exact supporting location, written as one of {LOCATION_GRAMMAR}"
            ]
        span = location.line_count
        if span is not None and span > MAXIMUM_LOCATION_LINES:
            return [
                f"exact_location spans {span} lines, wider than the {MAXIMUM_LOCATION_LINES}-line "
                "limit; a range that wide is a search, not an exact supporting location"
            ]
        return []

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


def _resolve_pointer(pointer: str) -> Path:
    """Resolve a pointer to a file inside this repository, or refuse.

    A citation names evidence in the repository under audit. Reading outside it
    is not a stricter check, it is a different one: the host's ``/etc`` is not
    reviewable, not committed, not hashed at a known revision, and differs per
    machine, so a citation to it can never be reproduced by anyone else. Both
    ways out — a leading ``/`` and a ``..`` walk — are refused, and the refusal
    is after ``resolve()`` so a symlink out is refused too.
    """
    raw = str(pointer or "").strip()
    if not raw:
        raise SourceUnreadable("empty source pointer")
    if Path(raw).is_absolute():
        raise SourceUnreadable(
            f"{pointer}: absolute pointers are refused; cite a path relative to the repository root"
        )
    path = (REPO_ROOT / raw).resolve()
    if not path.is_relative_to(REPO_ROOT):
        raise SourceUnreadable(
            f"{pointer}: resolves to {path}, outside the repository; a citation must point at "
            "evidence anyone can re-read at a known revision"
        )
    if not path.is_file():
        raise SourceUnreadable(f"{pointer} does not resolve to a file on this host")
    return path


def read_source_bytes(pointer: str) -> bytes:
    """Resolve a source pointer to its raw bytes.

    Bytes, not text, because the content hash must be over what is actually on
    disk. Decoding first and hashing the result loses information: two files
    differing only in bytes that decode to the same replacement character hash
    identically, which defeats staleness detection exactly where it matters.

    Only local pointers resolve here. A remote URL must have been snapshotted
    and hashed at retrieval (§16.1) and cited by its snapshot path — verifying
    a live URL at check time would be checking a *different* document from the
    one the claim was made against, and would silently pass when the page had
    changed underneath it.
    """
    path = _resolve_pointer(pointer)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SourceUnreadable(f"{pointer}: {exc}") from exc


def decode_source(pointer: str, data: bytes) -> str:
    """Decode source bytes as UTF-8, strictly.

    Strictly, and with the encoding named, for two reasons. Naming it makes the
    result host-independent — ``read_text()`` without an encoding follows the
    machine's locale, so the same repository could verify on one host and not on
    another. Failing rather than substituting replacement characters makes a
    binary source say so: before this, a ``.pyc`` decoded to mojibake, missed
    the quote or mismatched the hash, and was reported as STALE — a claim about
    a file having *changed*, which nobody had checked and which was usually
    false. UNRESOLVABLE is the honest status: the pointer resolves, but a
    byte-exact quote check is not defined over these bytes.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnreadable(
            f"{pointer}: not UTF-8 text ({exc}); a quote cannot be checked against a "
            "binary source, and reporting it as changed would be a claim nobody verified"
        ) from exc


def read_source(pointer: str) -> str:
    """Resolve a source pointer to decoded text. Raises for binary sources."""
    return decode_source(pointer, read_source_bytes(pointer))


def verify_citation(citation: Citation, *, reader=read_source_bytes) -> CitationCheck:
    """Re-read the source and check the quote is really there, where it says.

    Deterministic. No model participates. This is the check that makes a
    fabricated citation fail rather than pass. *reader* must return ``bytes``:
    the hash is over what was read, so a reader that returns text has already
    made a decoding decision this function would have to trust.
    """
    structural = citation.structural_findings()
    if structural:
        return CitationCheck(citation, CitationStatus.MALFORMED, "; ".join(structural))

    try:
        data = reader(citation.source_pointer)
        if not isinstance(data, bytes):
            raise TypeError(f"reader returned {type(data).__name__}; verify_citation requires bytes")
        text = decode_source(citation.source_pointer, data)
    except SourceUnreadable as exc:
        return CitationCheck(citation, CitationStatus.UNRESOLVABLE, str(exc))

    observed = "sha256:" + hashlib.sha256(data).hexdigest()
    quote = _normalise(citation.quote)

    if quote not in _normalise(text):
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

    # The location is checked, not echoed. A citation whose quote is somewhere
    # in a long file but nowhere near the section it names has not shown what it
    # says it shows, and the pre-hardening version of this function reported
    # that case as VERIFIED with the unchecked location quoted back in the
    # detail string.
    location = parse_location(citation.exact_location)
    span = locate(text, location) if location is not None else None
    if span is None:
        return CitationCheck(
            citation,
            CitationStatus.UNSUPPORTED,
            (
                f"the cited location {citation.exact_location!r} does not exist in "
                f"{citation.source_pointer}; the quote occurs elsewhere in the file, but the "
                "citation points at nothing"
            ),
            observed,
        )
    if quote not in _normalise(span):
        return CitationCheck(
            citation,
            CitationStatus.UNSUPPORTED,
            (
                f"the quoted text occurs in {citation.source_pointer} but not at "
                f"{citation.exact_location!r}; the cited location does not contain it"
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

    ``load_bearing`` records the author's intent and is reported in the body. It
    buys nothing: :func:`validate_claim` applies the same evidence rules to every
    claim, so an incidental claim with no citations is ``INSUFFICIENT_EVIDENCE``
    rather than ``SUPPORTED``. It used to be a bypass — an uncited claim marked
    incidental returned ``SUPPORTED`` at T4, which made ``load_bearing=False``
    the cheapest way to launder an unevidenced statement into the tier system.
    ``SUPPORTED`` now means "the evidence was checked and held", never "there
    was nothing to check".
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


def validate_claim(claim: Claim, *, reader=read_source_bytes) -> ClaimValidation:
    """§15.4's "citation and claim validation", as a deterministic check.

    The order of the tests below is the order of interest, not convenience: a
    fabricated quote outranks a broken record, a broken record outranks a weak
    one, and staleness is reported last because it is the only failure that says
    the claim may once have been true.
    """
    checks = [verify_citation(c, reader=reader) for c in claim.citations]
    findings: list[str] = []

    if not claim.citations:
        if claim.is_load_bearing:
            findings.append(
                "a load-bearing claim with no citation; §7.3 requires a source record "
                "for every load-bearing claim"
            )
        else:
            findings.append(
                "an uncited claim; it may be true, but nothing here shows it, and "
                "'incidental' is not evidence"
            )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    fabricated = [c for c in checks if c.status is CitationStatus.UNSUPPORTED]
    if fabricated:
        findings.extend(f"{c.citation.source_id}: {c.detail}" for c in fabricated)
        return ClaimValidation(claim, ClaimVerdict.UNSUPPORTED, checks, findings)

    # A malformed or unresolvable citation used to be recorded and then ignored,
    # so one good citation carried a claim that also carried an empty one. An
    # evidence record with an invalid entry in it has not been checked; it has
    # been partly checked, and the honest verdict for partly checked is that the
    # evidence is insufficient.
    unchecked = [
        c
        for c in checks
        if c.status in (CitationStatus.MALFORMED, CitationStatus.UNRESOLVABLE)
    ]
    if unchecked:
        findings.extend(f"{c.citation.source_id}: {c.detail}" for c in unchecked)
        findings.append(
            "a claim is supported only when every citation on it checks out; "
            f"{len(unchecked)} of {len(checks)} did not"
        )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    verified = [c for c in checks if c.counts_as_support]
    if not verified:
        findings.append(
            "no citation verified against its source; the claim may be true but "
            "nothing here shows it"
        )
        stale = [c for c in checks if c.status is CitationStatus.STALE]
        return ClaimValidation(
            claim,
            ClaimVerdict.STALE if stale else ClaimVerdict.INSUFFICIENT_EVIDENCE,
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

    # The linkage floor. Deterministic, weak, and honest about being weak: see
    # "Why linkage is only a lexical overlap floor" in the module docstring.
    linked = [
        c
        for c in direct
        if len(statement_quote_overlap(claim.statement, c.citation.quote))
        >= MINIMUM_STATEMENT_QUOTE_OVERLAP
    ]
    if not linked:
        findings.append(
            "no verified quote shares a content word with the statement; the quotes are "
            "real but nothing mechanical connects them to what is being claimed. This is a "
            "lexical floor, not a relevance judgement — §7.3's gate forbids a model judge "
            "in this path, so relevance is not decidable here"
        )
        return ClaimValidation(claim, ClaimVerdict.INSUFFICIENT_EVIDENCE, checks, findings)

    if any(c.status is CitationStatus.STALE for c in checks):
        findings.append("at least one source has changed since retrieval; revalidate (§15.7)")
        return ClaimValidation(claim, ClaimVerdict.STALE, checks, findings)

    return ClaimValidation(claim, ClaimVerdict.SUPPORTED, checks, findings)


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

    A convenience for the common case, and the hash is taken from the file's raw
    bytes rather than accepted from a caller — a caller-supplied hash is a
    caller-supplied claim, which is the thing being checked.
    """
    data = read_source_bytes(path)
    return Citation(
        source_id=source_id,
        source_pointer=path,
        source_class=source_class,
        retrieved_at=date.today().isoformat(),
        exact_location=exact_location,
        quote=quote,
        support_kind=support_kind,
        applicability=applicability,
        content_hash="sha256:" + hashlib.sha256(data).hexdigest(),
        retrieval_provenance=f"read from the working tree at {REPO_ROOT}",
        inference_step=inference_step,
    )
