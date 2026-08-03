"""GATE-D2-14 — competing hypotheses and a discriminating test.

Contract Section 7.4. The gate exists because agents reach for the first
plausible fix, prove nothing, and let the same bug come back. The discipline it
enforces is competing hypotheses, each with its own predicted observations, and
a test whose outcome could only have come out one way. **The target is not
finding bugs faster. It is making a coincidental fix unable to pass.** Every
judgement in this module is made against that sentence, and two of the four
assertions are staged to ``UNVERIFIABLE`` because the current record shape
cannot hold anyone to it.

``model_judge_in_verdict_path: false``. Nothing here calls a model; the
verdicts are string, set and interval arithmetic over recorded artifacts.

Four things shaped these checks. The first two are traps that a plausible
implementation walks straight into, and each one would have produced a green
that measured nothing.

* **Trap 1 — the contract ships the template with every list empty.**
  ``contract.md`` Section 7.4 gives the eight-field shape as a fenced YAML
  block in which ``supporting_evidence``, ``contradicting_evidence``,
  ``discriminating_tests`` and ``expected_observations`` are all ``[]``. A check
  reading ``all_eight_fields_present`` as "the eight keys are there" therefore
  **passes a verbatim copy of the owner's own template** -- eight keys, zero
  content. :func:`d2_14_a2` requires presence *and* non-emptiness, reusing
  :func:`oracles.base.is_placeholder` so ``"..."`` and ``TODO`` are refused on
  the same footing as ``[]``, and its negative control is the template itself,
  parsed out of ``contract.md`` at run time. If that control ever stops firing,
  the check has become decorative and says so.

* **Trap 2 — pairwise-distinct predictions is not decidable on this shape.**
  Distinct predictions are the property that actually makes a coincidental fix
  fail: if two hypotheses predict the same observation, no outcome can separate
  them and whichever fix was tried first "wins". But ``expected_observations``
  is free text in the contract and is **absent from every recorded hypothesis in
  this repository**. Over free text the only decidable notion of distinctness is
  string inequality, and string inequality passes five restatements of one
  theory. :func:`d2_14_a3` therefore does **not** implement a string-inequality
  check and call it discrimination. It runs that check on five restatements of a
  single theory, records that it reports "all distinct", and returns
  ``UNVERIFIABLE`` naming the shape it would need. The arithmetic predicate that
  *would* decide it once the owner amends the schema is implemented and
  exercised here (:func:`predictions_are_incompatible`,
  :func:`pairwise_discriminating`), so the day the amendment lands the staging
  comes off rather than the check being written from scratch.

* **A2 passing would not mean the hypotheses are distinct.** A2 is a shape
  assertion and can only ever be one; content that satisfies it can still be
  five paraphrases of one guess. That claim belongs to A3, and A3 is staged.
  Stated here because "the schema check is green" is exactly the false comfort
  this gate was written against.

* **"Not by discovery order" is decidable; "linked to the test result" is not.**
  A4 can measure real things today -- whether the selected hypothesis is the
  first one recorded with every alternative left ``open`` (the signature of
  discovery-order selection), whether exactly one is selected, whether the
  alternatives were ever disposed of. It cannot measure the link the assertion
  names, because the contract's own template has **no field in which to record
  one**: no ``selected_because``, no observation reference, nothing. So A4
  enumerates what it decided and returns ``UNVERIFIABLE`` for the rest, in the
  staging style of :func:`evaluation.checks_audit_followup.d2_10_a2`.

The proposed owner amendment that unstages A3 and A4 is written up in
``docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md``.
``project-pack/**`` is builder-read-only (``contracts/compiler.py`` sets
``read_only_paths``), so this module proposes and never applies.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from contracts import markdown
from evaluation.checks_audit_followup import _standard_evidence
from oracles.base import is_placeholder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext
    from evaluation.gate_spec import AssertionSpec, GateSpec


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; the three outcome constructors are the only runtime needs, and
# resolving them on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


def undecided(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import undecided as _undecided

    return _undecided(*args, **kwargs)


# ===========================================================================
# The declared shape, read from the owner's documents rather than restated
# ===========================================================================

#: Where a recorded hypothesis set could live. Scanned in full so "one artifact
#: carries hypotheses" is a measurement rather than an assumption about where
#: somebody would have put one.
CORPUS_ROOTS: tuple[str, ...] = ("evidence", "docs", "project-pack")
CORPUS_SUFFIXES: tuple[str, ...] = (".json", ".yaml", ".yml")

#: The key a recorded hypothesis set hangs off.
HYPOTHESIS_LIST_KEY = "hypotheses"

#: The four declared fields that carry the argument rather than label it. These
#: are the ones the contract's template ships empty, so these are the ones a
#: presence-only check would wave through. See Trap 1 in the module docstring.
LOAD_BEARING_LISTS: tuple[str, ...] = (
    "supporting_evidence",
    "contradicting_evidence",
    "discriminating_tests",
    "expected_observations",
)

#: The ``status`` value that marks a hypothesis as the one acted on. Checked
#: against the contract's own enum before it is used, so a renamed enum member
#: fails loudly instead of making A4 look at an empty set.
SELECTED_STATUS = "supported"

#: Keys A4 searches for a machine-followable link from a selection to the
#: observation that forced it. None of them exists in the contract's Section 7.4
#: template; the list is written out so the evidence records what was looked for
#: rather than only that nothing was found.
SELECTION_PROVENANCE_KEYS: tuple[str, ...] = (
    "selected_because",
    "selection_provenance",
    "selected_by_test",
    "decided_by",
    "discriminating_test_id",
    "test_id",
    "test_result_ref",
    "linked_test_result",
    "observation_ref",
    "observed_ref",
    "evidence_ref",
)

#: Keys A3 searches for something a runner could execute or a reader could
#: re-run. Same rationale as above: the search is part of the evidence.
RUNNABLE_REFERENCE_KEYS: tuple[str, ...] = (
    "test_id",
    "runnable_ref",
    "command",
    "run_id",
    "exit_code",
    "artifact_hash",
    "executed_at",
    "result",
)

#: The comparators the proposed typed shape admits. Kept as a module constant so
#: :func:`predictions_are_incompatible` cannot silently accept a comparator it
#: has no arithmetic for.
COMPARATORS: frozenset[str] = frozenset({"<", "<=", "==", "!=", ">=", ">"})

CONTRACT_SECTION = "7.4 Hypothesis-based research and debugging"


def _declared_fields(ctx: GateContext) -> list[str]:
    """The eight, from ``methodology-policy.yaml`` -- the compiler's own source."""
    policy = ctx.pack_yaml("methodology-policy.yaml")
    return [str(f) for f in policy["hypothesis_discipline"]["required_fields"]]


def _declared_minimum(ctx: GateContext) -> int:
    policy = ctx.pack_yaml("methodology-policy.yaml")
    return int(policy["hypothesis_discipline"]["minimum_hypotheses_when_multiple_causes_credible"])


@functools.lru_cache(maxsize=4)
def _contract_template(repo_root: Path) -> dict[str, Any]:
    """The Section 7.4 template, parsed out of ``contract.md`` at run time.

    This is both the field list the contract states and -- unchanged, unedited --
    the negative control A2 owes itself. Reading it here rather than restating it
    means the control is the owner's document, so it cannot drift away from the
    thing it is meant to be a copy of.
    """
    text = (repo_root / "project-pack" / "contract.md").read_text()
    block = markdown.fenced_block(markdown.section(text, CONTRACT_SECTION))
    loaded = yaml.safe_load("\n".join(block)) if block else None
    return dict(loaded) if isinstance(loaded, dict) else {}


def _template_enums(template: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """``{"confidence": ("unknown", "low", ...)}`` from the template's own alternations."""
    return {
        field: tuple(part.strip() for part in value.split("|"))
        for field, value in template.items()
        if isinstance(value, str) and "|" in value
    }


# ===========================================================================
# The corpus
# ===========================================================================


@dataclass(frozen=True)
class HypothesisSet:
    """One recorded list of hypotheses, and where it was found."""

    source: str
    pointer: str
    records: tuple[dict[str, Any], ...]

    @property
    def label(self) -> str:
        return f"{self.source}{self.pointer}"


def _load_structured(path: Path) -> Any:
    text = path.read_text(errors="ignore")
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _walk(
    node: Any, pointer: str, found: list[tuple[str, tuple[dict[str, Any], ...]]], rejected: list[str]
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{pointer}.{key}"
            if key == HYPOTHESIS_LIST_KEY and isinstance(value, list):
                if value and all(isinstance(item, dict) for item in value):
                    found.append((child, tuple(value)))
                    continue
                rejected.append(child)
            _walk(value, child, found, rejected)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{pointer}[{index}]", found, rejected)


@dataclass(frozen=True)
class Corpus:
    sets: tuple[HypothesisSet, ...]
    files_scanned: int
    unreadable: tuple[str, ...]
    rejected: tuple[str, ...]


@functools.lru_cache(maxsize=4)
def _corpus(repo_root: Path) -> Corpus:
    """Every recorded hypothesis set in the repository, and the search that found it.

    The count of files scanned is carried because "one artifact records
    hypotheses" and "the scanner looked in one directory" are indistinguishable
    from the outside, and the first is a fact about the repository while the
    second is a fact about this function.
    """
    sets: list[HypothesisSet] = []
    unreadable: list[str] = []
    rejected: list[str] = []
    scanned = 0
    for root in CORPUS_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in CORPUS_SUFFIXES:
                continue
            scanned += 1
            relative = path.relative_to(repo_root).as_posix()
            try:
                document = _load_structured(path)
            except Exception as exc:  # pragma: no cover - a malformed artifact
                unreadable.append(f"{relative}: {type(exc).__name__}: {exc}")
                continue
            found: list[tuple[str, tuple[dict[str, Any], ...]]] = []
            local_rejected: list[str] = []
            _walk(document, "", found, local_rejected)
            rejected.extend(f"{relative}{p}" for p in local_rejected)
            sets.extend(HypothesisSet(relative, pointer, records) for pointer, records in found)
    return Corpus(tuple(sets), scanned, tuple(unreadable), tuple(rejected))


def _is_near_miss(key: str, field: str) -> bool:
    """Is ``key`` a recognisable misspelling of the declared ``field``?

    Token-aware on purpose. Plain substring matching calls ``id`` a near miss of
    ``supporting_evidence`` (``"...ev|iden|ce"``), ``contradicting_evidence`` and
    ``confidence``, and a finding that names the wrong culprit is worse than one
    that names none. The two shapes that actually occur are a dropped qualifier
    (``id`` for ``hypothesis_id``) and a lost plural (``discriminating_test``
    for ``discriminating_tests``); both are matched on whole ``_`` tokens.
    """
    if key == field:
        return False
    if key + "s" == field or key == field + "s":
        return True
    key_tokens = key.split("_")
    field_tokens = field.split("_")
    if len(key_tokens) >= len(field_tokens):
        return False
    span = len(key_tokens)
    return field_tokens[-span:] == key_tokens or field_tokens[:span] == key_tokens


def _record_label(record: dict[str, Any], index: int) -> str:
    for key in ("hypothesis_id", "id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"#{index + 1}"


# ===========================================================================
# The predicates. Each one is run over the live corpus *and* over the negative
# controls, so a control that passes is a control that passed the same code.
# ===========================================================================


def count_findings(subject: HypothesisSet, minimum: int) -> list[str]:
    """A1's predicate: at least ``minimum`` hypotheses in a recorded set."""
    if len(subject.records) < minimum:
        return [
            f"{subject.label} records {len(subject.records)} hypothes"
            f"{'is' if len(subject.records) == 1 else 'es'}; Section 7.4 requires at least "
            f"{minimum} where more than one cause is credible, and a single recorded hypothesis "
            "is the first plausible fix wearing a form"
        ]
    return []


def schema_findings(
    subject: HypothesisSet, declared: list[str], enums: dict[str, tuple[str, ...]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """A2's predicate: all eight present, and the load-bearing ones non-empty.

    Presence alone is the trap. ``contract.md``'s own template has all eight keys
    and four empty lists, so a presence-only reading of
    ``all_eight_fields_present`` accepts a verbatim copy of it. Every declared
    field is therefore scored twice -- present, and not a placeholder -- and the
    two enumerated fields are scored a third time against the contract's enum.
    """
    findings: list[str] = []
    report: list[dict[str, Any]] = []
    for index, record in enumerate(subject.records):
        label = _record_label(record, index)
        absent = [field for field in declared if field not in record]
        empty = [
            field for field in declared if field in record and is_placeholder(record[field])
        ]
        # Near misses are reported, never accepted. ``id`` is not
        # ``hypothesis_id`` and ``discriminating_test`` is not
        # ``discriminating_tests``; treating either as the declared name would
        # smooth over the exact defect the assertion is about, and renaming a
        # field in an owner artifact is not this check's call. Naming them makes
        # the finding actionable without making it lenient.
        near_misses = {
            key: sorted(f for f in declared if _is_near_miss(key, f))
            for key in record
            if key not in declared and any(_is_near_miss(key, f) for f in declared)
        }
        enum_violations = {
            field: record[field]
            for field, allowed in enums.items()
            if field in record and str(record[field]) not in allowed
        }
        report.append(
            {
                "hypothesis": label,
                "keys_recorded": sorted(record),
                "declared_fields_absent": absent,
                "declared_fields_present_but_empty": empty,
                "keys_that_nearly_match_a_declared_field": near_misses,
                "values_outside_the_contract_enum": enum_violations,
            }
        )
        for field in absent:
            note = ""
            alias = next((k for k, v in near_misses.items() if field in v), None)
            if alias:
                note = (
                    f"; the record carries {alias!r}, which is not the declared name and is not "
                    "accepted as one -- renaming it is the owner's call, not this check's"
                )
            findings.append(f"{subject.label} {label}: required field {field!r} is absent{note}")
        for field in empty:
            findings.append(
                f"{subject.label} {label}: required field {field!r} is present but empty "
                f"({record[field]!r}); the contract ships this template with every list empty, so "
                "presence without content is a verbatim copy of the template and proves nothing"
            )
        for field, value in enum_violations.items():
            findings.append(
                f"{subject.label} {label}: {field}={value!r} is outside the contract's enum "
                f"{list(enums[field])}"
            )
    return findings, report


def selection_findings(
    subject: HypothesisSet, enums: dict[str, tuple[str, ...]]
) -> tuple[list[str], dict[str, Any]]:
    """A4's decidable half: was the selection forced, or was it merely first?

    What this decides:

    * exactly one hypothesis is marked selected;
    * the selected one is not the first recorded while every alternative was
      left ``open`` -- that pattern is the signature of discovery-order
      selection and is the thing the assertion names;
    * no alternative is still ``open``, because an alternative nobody disposed
      of is an alternative the test never ruled out.

    What it cannot decide is in :func:`d2_14_a4`: whether the selection is
    *linked* to a test result. There is no field for one.
    """
    statuses = [str(record.get("status", "")) for record in subject.records]
    selected = [index for index, status in enumerate(statuses) if status == SELECTED_STATUS]
    findings: list[str] = []

    if not selected:
        findings.append(
            f"{subject.label}: no hypothesis carries status={SELECTED_STATUS!r}, so there is no "
            "selection whose provenance could be checked"
        )
    elif len(selected) > 1:
        findings.append(
            f"{subject.label}: {len(selected)} hypotheses carry status={SELECTED_STATUS!r} "
            f"({[_record_label(subject.records[i], i) for i in selected]}); a discriminating test "
            "that leaves two survivors did not discriminate"
        )

    open_alternatives = [
        _record_label(subject.records[index], index)
        for index, status in enumerate(statuses)
        if index not in selected and (not status or status == "open")
    ]
    if selected and open_alternatives:
        findings.append(
            f"{subject.label}: alternatives {open_alternatives} are still 'open' while "
            f"{_record_label(subject.records[selected[0]], selected[0])} was selected; an "
            "alternative nobody disposed of is one the test never ruled out"
        )
    if len(selected) == 1 and selected[0] == 0 and len(subject.records) > 1 and open_alternatives:
        findings.append(
            f"{subject.label}: the selected hypothesis is the first one recorded and every "
            "alternative is still open -- indistinguishable from implementing the first plausible "
            "fix because it was found first, which is what Section 7.4 forbids"
        )

    report = {
        "set": subject.label,
        "recorded_order": [
            _record_label(record, index) for index, record in enumerate(subject.records)
        ],
        "statuses": statuses,
        "statuses_outside_the_contract_enum": [
            status for status in statuses if status not in enums.get("status", ())
        ],
        "selected": [_record_label(subject.records[i], i) for i in selected],
        "selected_positions": selected,
        "selection_is_the_first_recorded": bool(selected) and selected[0] == 0,
        "alternatives_left_open": open_alternatives,
    }
    return findings, report


# ===========================================================================
# Distinctness: the inert one, and the one the amendment would make decidable
# ===========================================================================


def string_distinct_predictions(subject: HypothesisSet) -> dict[str, Any]:
    """The check this module deliberately does **not** use for its verdict.

    Over free text the only decidable notion of distinctness is string
    inequality. It is implemented here so A3's transcript can show it reporting
    "all distinct" over five restatements of a single theory -- which is the
    concrete demonstration that a string-inequality check cannot fail, and
    therefore that shipping one would be the decorative gate this project exists
    to prevent.
    """
    texts = [
        " ".join(str(v) for v in (record.get("expected_observations") or [record.get("claim", "")]))
        for record in subject.records
    ]
    pairs = [
        {"a": index_a, "b": index_b, "strings_differ": texts[index_a] != texts[index_b]}
        for index_a in range(len(texts))
        for index_b in range(index_a + 1, len(texts))
    ]
    return {
        "compared": texts,
        "pairs": pairs,
        "all_pairs_reported_distinct": all(pair["strings_differ"] for pair in pairs),
    }


@dataclass(frozen=True)
class TypedObservation:
    """One prediction in the shape the proposed amendment would require."""

    observable_id: str
    comparator: str
    value: float
    unit: str


def _fold(observation: TypedObservation, bounds: dict[str, Any]) -> None:
    comparator, value = observation.comparator, float(observation.value)
    if comparator in ("<", "<="):
        strict = comparator == "<"
        if value < bounds["hi"]:
            bounds["hi"], bounds["hi_open"] = value, strict
        elif value == bounds["hi"]:
            bounds["hi_open"] = bounds["hi_open"] or strict
    elif comparator in (">", ">="):
        strict = comparator == ">"
        if value > bounds["lo"]:
            bounds["lo"], bounds["lo_open"] = value, strict
        elif value == bounds["lo"]:
            bounds["lo_open"] = bounds["lo_open"] or strict
    elif comparator == "==":
        _fold(TypedObservation(observation.observable_id, ">=", value, observation.unit), bounds)
        _fold(TypedObservation(observation.observable_id, "<=", value, observation.unit), bounds)
    elif comparator == "!=":
        bounds["excluded"].add(value)


def predictions_are_incompatible(
    left: TypedObservation, right: TypedObservation
) -> tuple[bool, str]:
    """Can any real measurement satisfy both predictions at once?

    This is the arithmetic predicate the amendment buys. Two predictions on the
    same ``observable_id`` in the same ``unit`` are folded into an interval over
    the reals; if the intersection is empty, no run of the discriminating test
    can be consistent with both hypotheses, and the test discriminates them.
    ``!=`` is tracked as an excluded point, which can only empty the
    intersection when the interval has already collapsed to that point --
    the reals are dense, so it cannot do so otherwise.

    Returns ``(incompatible, why)``. The reason string is the whole value of
    the amendment: an outcome that can only be one way, stated as a number.
    """
    if left.observable_id != right.observable_id:
        return False, (
            f"different observables ({left.observable_id!r} vs {right.observable_id!r}); "
            "two predictions about different things cannot contradict each other"
        )
    if left.unit != right.unit:
        return False, (
            f"same observable {left.observable_id!r} recorded in {left.unit!r} and {right.unit!r}; "
            "a comparison across units is not a comparison"
        )
    unknown = {left.comparator, right.comparator} - COMPARATORS
    if unknown:
        return False, f"comparator(s) {sorted(unknown)} are outside {sorted(COMPARATORS)}"

    bounds: dict[str, Any] = {
        "lo": -math.inf,
        "lo_open": False,
        "hi": math.inf,
        "hi_open": False,
        "excluded": set(),
    }
    _fold(left, bounds)
    _fold(right, bounds)

    lo, hi = bounds["lo"], bounds["hi"]
    if lo > hi:
        empty = True
    elif lo == hi:
        empty = bounds["lo_open"] or bounds["hi_open"] or lo in bounds["excluded"]
    else:
        empty = False
    if empty:
        return True, (
            f"{left.observable_id} {left.comparator} {left.value}{left.unit} and "
            f"{right.observable_id} {right.comparator} {right.value}{right.unit} have no common "
            "satisfying measurement, so one run of the test refutes one of them"
        )
    return False, (
        f"{left.observable_id} {left.comparator} {left.value}{left.unit} and "
        f"{right.observable_id} {right.comparator} {right.value}{right.unit} are satisfied "
        "together by any measurement in the intersection; the same outcome supports both"
    )


def pairwise_discriminating(
    predictions: dict[str, list[TypedObservation]],
) -> tuple[bool, list[dict[str, Any]]]:
    """Every pair of hypotheses must predict incompatibly on some shared observable.

    This is what "a discriminating test distinguishes the hypotheses" becomes
    once ``expected_observations`` is typed: a decision procedure, not a reading
    of two paragraphs. It is the reason the proposed amendment is worth the
    owner's time, and it is exercised in A3's transcript so the claim that it
    works is not a claim.
    """
    names = sorted(predictions)
    pairs: list[dict[str, Any]] = []
    for index_a, left_name in enumerate(names):
        for right_name in names[index_a + 1 :]:
            reasons: list[str] = []
            incompatible = False
            for left in predictions[left_name]:
                for right in predictions[right_name]:
                    hit, why = predictions_are_incompatible(left, right)
                    if hit:
                        incompatible = True
                        reasons.append(why)
            pairs.append(
                {
                    "a": left_name,
                    "b": right_name,
                    "discriminated": incompatible,
                    "why": reasons
                    or [
                        "no shared observable on which the two predictions are mutually "
                        "unsatisfiable; no outcome of the test separates them"
                    ],
                }
            )
    return all(pair["discriminated"] for pair in pairs) and bool(pairs), pairs


# ===========================================================================
# Negative-control fixtures. Every one of these is an input that MUST fail.
# ===========================================================================


def _template_copy_set(repo_root: Path) -> HypothesisSet:
    """Two verbatim copies of ``contract.md``'s own Section 7.4 template.

    The exact input Trap 1 describes: eight keys, four empty lists, two
    alternation strings where an enum member belongs. A presence-only A2 passes
    it. This A2 must not.
    """
    template = dict(_contract_template(repo_root))
    return HypothesisSet(
        "<negative-control>",
        ".verbatim_contract_template",
        (dict(template), dict(template, hypothesis_id="H-002")),
    )


def _populated_record(hypothesis_id: str, claim: str, status: str = "open") -> dict[str, Any]:
    """A record that satisfies A2's predicate, so the predicate is shown selective."""
    return {
        "hypothesis_id": hypothesis_id,
        "claim": claim,
        "supporting_evidence": ["evidence/negative-control.json#observed"],
        "contradicting_evidence": ["evidence/negative-control.json#counter"],
        "discriminating_tests": ["tests/unit/test_negative_control.py::test_probe"],
        "expected_observations": ["the probe returns 200 on every attempt"],
        "confidence": "medium",
        "status": status,
    }


def _single_hypothesis_set() -> HypothesisSet:
    return HypothesisSet(
        "<negative-control>",
        ".one_hypothesis",
        (_populated_record("H-001", "the first thing anyone thought of", SELECTED_STATUS),),
    )


def _missing_field_set() -> HypothesisSet:
    record = _populated_record("H-001", "a claim with no prediction")
    record.pop("expected_observations")
    return HypothesisSet("<negative-control>", ".missing_expected_observations", (record,))


def _bad_enum_set() -> HypothesisSet:
    return HypothesisSet(
        "<negative-control>",
        ".status_outside_the_enum",
        (_populated_record("H-001", "a claim", "supported_then_refined"),),
    )


def _well_formed_set() -> HypothesisSet:
    return HypothesisSet(
        "<negative-control>",
        ".well_formed",
        (
            _populated_record("H-001", "the channel is down", "refuted"),
            _populated_record("H-002", "the channel rate-limits", SELECTED_STATUS),
        ),
    )


def _first_found_wins_set() -> HypothesisSet:
    """A selection with the exact signature the assertion forbids."""
    return HypothesisSet(
        "<negative-control>",
        ".first_found_wins",
        (
            _populated_record("H-001", "the first plausible fix", SELECTED_STATUS),
            _populated_record("H-002", "an alternative nobody ever tested", "open"),
            _populated_record("H-003", "another alternative nobody ever tested", "open"),
        ),
    )


def _two_survivors_set() -> HypothesisSet:
    return HypothesisSet(
        "<negative-control>",
        ".two_survivors",
        (
            _populated_record("H-001", "cause A", SELECTED_STATUS),
            _populated_record("H-002", "cause B", SELECTED_STATUS),
        ),
    )


#: Five restatements of one theory. Every string differs; every theory is the
#: same. This is the input a string-inequality distinctness check passes, and it
#: is why A3 does not ship one.
RESTATEMENTS_OF_ONE_THEORY: tuple[str, ...] = (
    "the channel rate-limits closely-spaced requests",
    "requests issued too close together are throttled by the channel",
    "the channel refuses calls that arrive back to back",
    "spacing the calls out avoids the failure, so it is a rate limit",
    "a per-channel rate limiter rejects bursts on this deployment",
)


def _restatement_set() -> HypothesisSet:
    return HypothesisSet(
        "<negative-control>",
        ".five_restatements_of_one_theory",
        tuple(
            _populated_record(f"H-00{index + 1}", claim) | {"expected_observations": [claim]}
            for index, claim in enumerate(RESTATEMENTS_OF_ONE_THEORY)
        ),
    )


# ===========================================================================
# A1 — at least two hypotheses
# ===========================================================================


def d2_14_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``hypothesis_count_check`` -- expected ``count >= 2``.

    Decidable today, and decided. Every recorded hypothesis set in the
    repository is counted against ``methodology-policy.yaml``'s own minimum, the
    minimum is reconciled against the number the gate's ``expected`` string
    states, and an empty corpus fails rather than passing vacuously -- "every
    recorded set has at least two" is true of a repository with no sets at all.

    **Honest limit, and it is the reason this gate has teeth only where somebody
    already did the work:** the assertion quantifies over "a research task with
    credible alternatives", and nothing in this repository enumerates those. No
    module in ``src/`` produces an ``efah.hypothesis`` record -- the compiler
    declares the schema and nothing writes an instance -- so the denominator is
    unknowable and this check decides the numerator. It measures that every set
    somebody recorded holds at least two competing hypotheses. It cannot measure
    a debugging session that recorded none, and it says so here rather than
    letting a green imply otherwise.
    """
    corpus = _corpus(ctx.repo_root)
    minimum = _declared_minimum(ctx)
    findings: list[str] = []

    expected_number = "".join(ch for ch in str(a.expected) if ch.isdigit())
    if expected_number and int(expected_number) != minimum:
        findings.append(
            f"the gate expects {a.expected!r} while methodology-policy.yaml sets "
            f"minimum_hypotheses_when_multiple_causes_credible={minimum}; the count below is "
            "being scored against a threshold the gate does not state"
        )

    if not corpus.sets:
        findings.append(
            f"no recorded hypothesis set was found in {list(CORPUS_ROOTS)} across "
            f"{corpus.files_scanned} structured files; 'every set records at least "
            f"{minimum}' over an empty corpus is a statement about an empty search"
        )

    per_set: list[dict[str, Any]] = []
    for subject in corpus.sets:
        subject_findings = count_findings(subject, minimum)
        per_set.append(
            {
                "set": subject.label,
                "hypotheses": len(subject.records),
                "ids": [_record_label(r, i) for i, r in enumerate(subject.records)],
                "meets_the_minimum": not subject_findings,
            }
        )
        findings.extend(subject_findings)

    single = count_findings(_single_hypothesis_set(), minimum)
    well_formed = count_findings(_well_formed_set(), minimum)
    empty_corpus_control = count_findings(
        HypothesisSet("<negative-control>", ".empty", ()), minimum
    )
    if not single:
        findings.append(
            "negative control did not fire: a set holding one hypothesis was accepted by the "
            "count predicate, so the count is not being applied"
        )
    if not empty_corpus_control:
        findings.append(
            "negative control did not fire: a set holding zero hypotheses was accepted by the "
            "count predicate"
        )
    if well_formed:
        findings.append(
            "negative control failed: a well-formed two-hypothesis set was rejected by the count "
            f"predicate ({well_formed}); a predicate that rejects everything counts nothing"
        )

    execution_log = {
        "check": a.method or "hypothesis_count_check",
        "expected": a.expected,
        "minimum_from_the_pack": minimum,
        "policy_source": "project-pack/methodology-policy.yaml#hypothesis_discipline",
        "corpus": {
            "roots": list(CORPUS_ROOTS),
            "suffixes": list(CORPUS_SUFFIXES),
            "structured_files_scanned": corpus.files_scanned,
            "hypothesis_sets_found": len(corpus.sets),
            "unreadable": list(corpus.unreadable),
            "candidates_rejected_as_malformed": list(corpus.rejected),
        },
        "per_set": per_set,
        "what_this_does_not_decide": (
            "the assertion quantifies over research tasks with credible alternatives, and nothing "
            "enumerates those: no module in src/ emits an efah.hypothesis record, so a debugging "
            "session that recorded no hypotheses at all is invisible to this check. What is "
            "decided is that every set somebody did record holds at least the pack's minimum."
        ),
    }
    negative_control = {
        "probe": (
            "run the same count predicate over a set holding exactly one hypothesis, over a set "
            "holding none, and over a well-formed two-hypothesis set"
        ),
        "why": (
            "'every recorded set has at least two' is true of a repository with no recorded sets, "
            "and of a predicate that never counts. The first two probes must be rejected and the "
            "third must be accepted, or the number this check reports is not being applied."
        ),
        "one_hypothesis_rejected": single,
        "zero_hypotheses_rejected": empty_corpus_control,
        "well_formed_pair_accepted": not well_formed,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{len(corpus.sets)} recorded hypothesis set(s) found across {corpus.files_scanned} "
            f"structured files, and each holds at least the {minimum} competing hypotheses "
            "methodology-policy.yaml requires; the count predicate rejects a one-hypothesis and a "
            "zero-hypothesis set and accepts a well-formed pair"
        ),
    )


# ===========================================================================
# A2 — all eight fields, present and non-empty
# ===========================================================================


def d2_14_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A2 ``hypothesis_schema_assert`` -- expected ``all_eight_fields_present``.

    Read literally, ``all_eight_fields_present`` is satisfied by a verbatim copy
    of the contract's own Section 7.4 template: eight keys, four empty lists, and
    two alternation strings standing in for enum members. That is Trap 1, and the
    control below is that exact document, parsed out of ``contract.md`` at run
    time so the control cannot drift from the thing it copies. Presence is
    therefore scored together with non-emptiness -- via
    :func:`oracles.base.is_placeholder`, the repository's own predicate, so
    ``"..."``, ``TODO`` and ``[]`` are refused on the same footing -- and the two
    enumerated fields are scored against the enums the template itself states.

    **What a green here would and would not mean.** It would mean every recorded
    hypothesis carries the eight fields with content in them. It would *not* mean
    the hypotheses compete: eight populated fields can hold five paraphrases of
    one guess. That is A3's claim, and A3 is staged UNVERIFIABLE because the
    current shape cannot decide it. Anyone reading a green A2 as "the hypothesis
    discipline is enforced" has read it wrong, which is why this paragraph is
    here and not in a commit message.

    The check also records, as evidence rather than as a finding, that the
    eight-field schema is compiled into an ``efah.artifact_schema`` object and
    enforced by nothing: no validator, no model, no producer. That is the
    structural reason the recorded records look the way they do, but it is not
    what this assertion claims, so it does not drive the verdict.
    """
    corpus = _corpus(ctx.repo_root)
    declared = _declared_fields(ctx)
    template = _contract_template(ctx.repo_root)
    enums = _template_enums(template)
    findings: list[str] = []

    if len(declared) != 8:
        findings.append(
            f"methodology-policy.yaml declares {len(declared)} required hypothesis fields "
            f"({declared}), but the assertion is 'all_eight_fields_present'; the policy and the "
            "gate disagree about what is being counted"
        )
    if sorted(template) != sorted(declared):
        findings.append(
            f"contract.md#7.4's template declares {sorted(template)} while "
            f"methodology-policy.yaml declares {sorted(declared)}; the two statements of the same "
            "schema disagree, so one of them is not the schema"
        )
    for field in LOAD_BEARING_LISTS:
        if field not in declared:
            findings.append(
                f"{field!r} is treated here as load-bearing but is not among the declared fields "
                f"{declared}; the non-emptiness rule is being applied to a field the contract "
                "does not require"
            )

    if not corpus.sets:
        findings.append(
            f"no recorded hypothesis set was found across {corpus.files_scanned} structured "
            "files; 'all eight fields present' over zero records is vacuous"
        )

    per_set: list[dict[str, Any]] = []
    for subject in corpus.sets:
        subject_findings, report = schema_findings(subject, declared, enums)
        per_set.append({"set": subject.label, "records": report})
        findings.extend(subject_findings)

    # --- the controls -----------------------------------------------------
    template_control, template_report = schema_findings(
        _template_copy_set(ctx.repo_root), declared, enums
    )
    missing_control, _ = schema_findings(_missing_field_set(), declared, enums)
    enum_control, _ = schema_findings(_bad_enum_set(), declared, enums)
    populated_control, _ = schema_findings(_well_formed_set(), declared, enums)

    template_empties = [f for f in template_control if "present but empty" in f]
    if not template_empties:
        findings.append(
            "negative control did not fire: a verbatim copy of contract.md#7.4's own template -- "
            "eight keys, every list empty -- was not rejected for emptiness. A presence-only "
            "schema check passes that document, which makes this assertion inert by construction"
        )
    if not missing_control:
        findings.append(
            "negative control did not fire: a record with expected_observations removed entirely "
            "was accepted, so presence is not being checked"
        )
    if not enum_control:
        findings.append(
            "negative control did not fire: a record whose status is outside the contract's enum "
            "was accepted"
        )
    if populated_control:
        findings.append(
            "negative control failed: a fully populated, enum-conformant pair was rejected "
            f"({populated_control}); a predicate that rejects everything proves nothing about the "
            "records it rejected"
        )

    # This module names the schema id too, and counting itself would let the
    # gate report its own existence as enforcement -- the exact circularity the
    # evidence is meant to expose.
    this_module = Path(__file__).resolve()
    enforcement_sites = sorted(
        path.relative_to(ctx.repo_root).as_posix()
        for path in (ctx.repo_root / "src").rglob("*.py")
        if path.resolve() != this_module and "efah.hypothesis" in path.read_text(errors="ignore")
    )

    execution_log = {
        "check": a.method or "hypothesis_schema_assert",
        "expected": a.expected,
        "declared_fields": declared,
        "declared_field_count": len(declared),
        "policy_source": "project-pack/methodology-policy.yaml#hypothesis_discipline.required_fields",
        "contract_template": template,
        "enums_read_from_the_template": {k: list(v) for k, v in enums.items()},
        "load_bearing_lists": list(LOAD_BEARING_LISTS),
        "per_set": per_set,
        "schema_declared_but_unenforced": {
            "compiled_as": "efah.artifact_schema -> target_schema_id efah.hypothesis",
            "src_files_that_mention_the_schema_id": enforcement_sites,
            "note": (
                "the eight fields are compiled from methodology-policy.yaml into an artifact "
                "schema object and validated by nothing -- no pydantic model, no producer, no "
                "gate other than this one. Recorded as evidence, not as a finding: this "
                "assertion is about the records, not about who enforces them."
            ),
        },
        "what_a_green_here_would_not_mean": (
            "eight populated fields can hold five paraphrases of one guess. Whether the "
            "hypotheses actually compete is A3's claim, and A3 is staged UNVERIFIABLE."
        ),
    }
    negative_control = {
        "probe": (
            "score a verbatim copy of contract.md#7.4's own template (eight keys, every list "
            "empty), a record with expected_observations deleted, a record whose status is "
            "outside the enum, and a fully populated conformant pair"
        ),
        "why": (
            "the contract ships the template empty, so 'all eight fields present' read as key "
            "presence accepts the owner's blank form and reports the discipline as satisfied by a "
            "copy-paste. The template control is the exact input that must fail; the populated "
            "pair is the input that must pass, or the predicate is refusing everything."
        ),
        "verbatim_template_rejected_for_emptiness": template_empties,
        "verbatim_template_report": template_report,
        "deleted_field_rejected": missing_control,
        "out_of_enum_status_rejected": enum_control,
        "populated_pair_accepted": not populated_control,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"every recorded hypothesis carries all {len(declared)} declared fields with content "
            "in each load-bearing list and enum-conformant confidence and status; a verbatim copy "
            "of the contract's own empty template is rejected, and a populated pair is accepted"
        ),
    )


# ===========================================================================
# A3 — a discriminating test, present and executed (STAGED)
# ===========================================================================

#: A worked example of the typed shape the proposed amendment would require,
#: used to show that pairwise distinctness becomes arithmetic the moment
#: ``expected_observations`` stops being prose. Both sets describe FINDING-008's
#: real situation; only the first one actually discriminates.
_DISCRIMINATING_TYPED_SET: dict[str, list[TypedObservation]] = {
    "H-002 the channel is dead": [
        TypedObservation("raw_call_success_rate", "==", 0.0, "ratio"),
    ],
    "H-006 the channel rate-limits bursts": [
        TypedObservation("raw_call_success_rate", ">=", 1.0, "ratio"),
    ],
}

_NON_DISCRIMINATING_TYPED_SET: dict[str, list[TypedObservation]] = {
    "H-006 the channel rate-limits bursts": [
        TypedObservation("raw_call_success_rate", ">", 0.9, "ratio"),
    ],
    "H-006' a per-channel throttle rejects bursts": [
        TypedObservation("raw_call_success_rate", ">=", 0.95, "ratio"),
    ],
}


def d2_14_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``discriminating_test_presence`` -- expected ``test_present_and_executed``.

    **Staged deliberately: this returns UNVERIFIABLE, never PASS, and the reason
    is a schema gap the owner has to close, not a bug this module can fix.**

    Three questions hide inside ``test_present_and_executed``, and they have
    three different answers on the current shape.

    1. *Present.* Decidable, and already A2's finding: the declared field is
       ``discriminating_tests`` and no recorded hypothesis carries it. Failing
       A3 for that would report one defect twice, so it is measured and shown
       here and left to drive A2's verdict alone.
    2. *Executed.* Not decidable. A discriminating test is recorded as a prose
       sentence. There is no test id, no runnable reference, no command, no exit
       status, no artifact hash -- nothing a runner could execute or a reader
       could re-run. Deciding "executed" from prose is reading comprehension,
       and reading comprehension in a verdict path is the model judge this gate
       forbids.
    3. *Distinguishes the hypotheses.* Not decidable, and this is the one that
       matters. ``expected_observations`` is free text in the contract and is
       absent from every recorded hypothesis. **Over free text the only decidable
       notion of distinctness is string inequality, and string inequality passes
       five restatements of one theory.** The transcript proves that rather than
       asserting it: :func:`string_distinct_predictions` is run over five
       differently-worded statements of a single claim and reports every pair
       distinct. A check that cannot fail on that input is decorative, and
       shipping one here would be precisely the failure this gate exists to
       catch.

    What this check does instead of pretending: it measures every decidable
    thing, it demonstrates the inert check being inert, and it carries the
    predicate that *would* decide (3). :func:`pairwise_discriminating` is run
    over a typed set that genuinely discriminates and over one that does not,
    and it separates them arithmetically. The day
    ``docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md`` is
    accepted, unstaging this assertion is replacing the ``undecided`` at the end
    of this function with a verdict over real typed records.
    """
    corpus = _corpus(ctx.repo_root)
    declared = _declared_fields(ctx)
    findings: list[str] = []
    per_set: list[dict[str, Any]] = []

    declared_test_field = "discriminating_tests"
    typed_predictions_found = 0

    for subject in corpus.sets:
        records: list[dict[str, Any]] = []
        for index, record in enumerate(subject.records):
            tests = record.get(declared_test_field)
            near = sorted(
                key
                for key in record
                if key != declared_test_field and "discriminat" in key.lower()
            )
            entries = tests if isinstance(tests, list) else []
            runnable = sorted(
                key
                for entry in entries
                if isinstance(entry, dict)
                for key in entry
                if key in RUNNABLE_REFERENCE_KEYS
            )
            observations = record.get("expected_observations")
            typed = [
                item
                for item in (observations if isinstance(observations, list) else [])
                if isinstance(item, dict) and "observable_id" in item and "comparator" in item
            ]
            typed_predictions_found += len(typed)
            records.append(
                {
                    "hypothesis": _record_label(record, index),
                    "declared_field_present": declared_test_field in record,
                    "near_miss_keys": near,
                    "recorded_test_is_prose": bool(near)
                    and all(isinstance(record[key], str) for key in near),
                    "runnable_reference_keys_found": runnable,
                    "expected_observations_present": "expected_observations" in record,
                    "typed_expected_observations": len(typed),
                }
            )
        per_set.append({"set": subject.label, "records": records})

    string_probe = string_distinct_predictions(_restatement_set())
    if not string_probe["all_pairs_reported_distinct"]:
        findings.append(
            "the inert-check demonstration did not behave as documented: string inequality "
            "reported a pair of restatements as identical, so the transcript below no longer "
            "shows what it claims to show"
        )

    discriminating, discriminating_pairs = pairwise_discriminating(_DISCRIMINATING_TYPED_SET)
    non_discriminating, non_discriminating_pairs = pairwise_discriminating(
        _NON_DISCRIMINATING_TYPED_SET
    )
    if not discriminating:
        findings.append(
            "the proposed arithmetic predicate failed its own worked example: a typed set whose "
            f"predictions are mutually unsatisfiable was not separated ({discriminating_pairs})"
        )
    if non_discriminating:
        findings.append(
            "the proposed arithmetic predicate separated a typed set whose predictions are "
            f"satisfied by the same measurement ({non_discriminating_pairs}); it is not deciding "
            "incompatibility"
        )

    execution_log = {
        "check": a.method or "discriminating_test_presence",
        "expected": a.expected,
        "declared_field": declared_test_field,
        "declared_fields": declared,
        "per_set": per_set,
        "typed_expected_observations_in_the_whole_repository": typed_predictions_found,
        "the_three_questions": {
            "present": (
                "decidable, and it is A2's finding: no recorded hypothesis carries the declared "
                "field 'discriminating_tests'. Measured here and left to drive A2's verdict, "
                "because failing both would report one defect twice."
            ),
            "executed": (
                "not decidable: a discriminating test is recorded as a prose sentence with no "
                f"test id, runnable reference, command, exit status or artifact hash (searched "
                f"for {list(RUNNABLE_REFERENCE_KEYS)}). Deciding 'executed' from prose is reading "
                "comprehension, and this gate sets model_judge_in_verdict_path: false."
            ),
            "distinguishes_the_hypotheses": (
                "not decidable: expected_observations is free text in contract.md#7.4 and absent "
                "from every recorded hypothesis. Over free text the only decidable distinctness "
                "is string inequality, which passes five restatements of one theory -- see the "
                "transcript."
            ),
        },
        "staged": "reported UNVERIFIABLE, not PASS and not FAIL, pending the owner amendment",
        "amendment": "docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md",
    }
    negative_control = {
        "probe": (
            "run the only distinctness predicate the current free-text shape admits -- string "
            "inequality -- over five differently-worded restatements of one theory; then run the "
            "arithmetic predicate the proposed typed shape admits over a typed set that "
            "discriminates and one that does not"
        ),
        "why": (
            "a negative control is supposed to fire. This one cannot, and that is the finding. "
            "Five restatements of a single claim are five distinct strings, so a string-inequality "
            "check reports them as five competing hypotheses and a coincidental fix passes. The "
            "second probe shows the same question is decidable the moment the predictions are "
            "typed, which is what the amendment asks for."
        ),
        "inert_check_that_cannot_fire": {
            "input": list(RESTATEMENTS_OF_ONE_THEORY),
            "restatements_of": "one theory: the channel rate-limits closely-spaced requests",
            "string_inequality_reports_all_pairs_distinct": string_probe[
                "all_pairs_reported_distinct"
            ],
            "pairs": string_probe["pairs"],
            "conclusion": (
                "this is why A3 does not ship a string-inequality check. It would be green on "
                "this input, and this input is the exact failure the gate exists to prevent."
            ),
        },
        "arithmetic_predicate_under_the_proposed_typed_shape": {
            "discriminating_set": {
                "predictions": {
                    name: [vars(o) for o in observations]
                    for name, observations in _DISCRIMINATING_TYPED_SET.items()
                },
                "pairwise_discriminating": discriminating,
                "pairs": discriminating_pairs,
            },
            "non_discriminating_set": {
                "predictions": {
                    name: [vars(o) for o in observations]
                    for name, observations in _NON_DISCRIMINATING_TYPED_SET.items()
                },
                "pairwise_discriminating": non_discriminating,
                "pairs": non_discriminating_pairs,
            },
        },
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)

    # Staged: the decidable parts are recorded, the undecidable parts are named.
    return undecided(
        "UNVERIFIABLE: 'test_present_and_executed' cannot be honestly decided on the current "
        "record shape. Presence is decidable and is already A2's finding (no recorded hypothesis "
        "carries the declared field 'discriminating_tests'); 'executed' is not decidable because "
        "a discriminating test is recorded as prose with no test id, runnable reference, command "
        "or exit status; and 'distinguishes the hypotheses' is not decidable at all because "
        "expected_observations is free text and absent from every record, so the only available "
        "distinctness predicate is string inequality -- which the transcript shows passing five "
        f"restatements of one theory ({len(RESTATEMENTS_OF_ONE_THEORY)} distinct strings, one "
        "claim). The arithmetic predicate that would decide it is implemented and demonstrated "
        "here; it needs typed expected_observations, of which this repository contains "
        f"{typed_predictions_found}. See "
        "docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md.",
        evidence,
    )


# ===========================================================================
# A4 — the selection is linked to the test outcome (STAGED)
# ===========================================================================


def d2_14_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``selection_provenance_check`` -- expected ``selection_linked_to_test_result``.

    **Staged deliberately: this returns UNVERIFIABLE, never PASS.** Half of the
    assertion is decidable today and is decided; the other half asks for a link
    the contract's own template gives no field to record.

    Decided here, over every recorded hypothesis set, by
    :func:`selection_findings`:

    * exactly one hypothesis is marked selected;
    * the selection is not the first one recorded with every alternative left
      ``open`` -- that pattern is the fingerprint of "implement the first
      plausible fix", and it is exactly what the assertion names;
    * no alternative is left ``open`` at all, because an alternative nobody
      disposed of is one the test never ruled out;
    * every status is inside the contract's enum.

    Not decidable, and the reason for the staging: ``selection_linked_to_test_result``
    needs a reference from the selected hypothesis to an observation. Section
    7.4's template has no such field -- no ``selected_because``, no
    ``observation_ref``, no test identifier -- so there is nowhere to record one
    and nothing to follow. The check searches for every name such a field might
    plausibly carry (:data:`SELECTION_PROVENANCE_KEYS`) and reports what it
    searched for alongside what it found, so the absence is a measurement.

    What it deliberately does **not** do is match prose against numbers found
    elsewhere in the same artifact. FINDING-008's selected hypothesis says
    ``"12/12 success at 15s spacing"`` and the same file's ``measurements`` block
    holds ``spaced_15s: {attempts: 12, ok: 12}``. Joining those requires parsing
    English into a key name. That is reading comprehension with a regular
    expression in front of it, it would break on the next artifact, and a gate
    whose verdict path does that has a judge in it under another name. Only
    exact key references are followed.
    """
    corpus = _corpus(ctx.repo_root)
    template = _contract_template(ctx.repo_root)
    enums = _template_enums(template)
    findings: list[str] = []

    if SELECTED_STATUS not in enums.get("status", ()):
        findings.append(
            f"{SELECTED_STATUS!r} is not among the contract's status enum "
            f"{list(enums.get('status', ()))}; the selection below is being looked for under a "
            "name the contract does not use"
        )
    if not corpus.sets:
        findings.append(
            f"no recorded hypothesis set was found across {corpus.files_scanned} structured "
            "files; 'the selection is linked to a test result' over zero selections is vacuous"
        )

    per_set: list[dict[str, Any]] = []
    linked_anywhere = 0
    for subject in corpus.sets:
        subject_findings, report = selection_findings(subject, enums)
        findings.extend(subject_findings)
        provenance = [
            {
                "hypothesis": _record_label(record, index),
                "provenance_keys_present": sorted(
                    key for key in record if key in SELECTION_PROVENANCE_KEYS
                ),
            }
            for index, record in enumerate(subject.records)
        ]
        linked_anywhere += sum(1 for entry in provenance if entry["provenance_keys_present"])
        per_set.append({**report, "selection_provenance": provenance})

    # --- the controls -----------------------------------------------------
    first_found, _ = selection_findings(_first_found_wins_set(), enums)
    two_survivors, _ = selection_findings(_two_survivors_set(), enums)
    no_selection, _ = selection_findings(
        HypothesisSet(
            "<negative-control>",
            ".nothing_selected",
            (_populated_record("H-001", "a"), _populated_record("H-002", "b")),
        ),
        enums,
    )
    well_formed, _ = selection_findings(_well_formed_set(), enums)

    if not any("found first" in f for f in first_found):
        findings.append(
            "negative control did not fire: a set whose first-recorded hypothesis is selected "
            "while every alternative is still open was not flagged as discovery-order selection"
        )
    if not two_survivors:
        findings.append(
            "negative control did not fire: a set with two hypotheses marked supported was "
            "accepted; a test that leaves two survivors did not discriminate"
        )
    if not no_selection:
        findings.append(
            "negative control did not fire: a set in which nothing is selected was accepted, so "
            "the selection is not being looked for"
        )
    if well_formed:
        findings.append(
            "negative control failed: a set whose selection is last, whose alternatives are all "
            f"refuted, and whose statuses are enum-conformant was rejected ({well_formed}); a "
            "predicate that rejects everything decides nothing"
        )

    execution_log = {
        "check": a.method or "selection_provenance_check",
        "expected": a.expected,
        "selected_status": SELECTED_STATUS,
        "status_enum": list(enums.get("status", ())),
        "per_set": per_set,
        "provenance_keys_searched_for": list(SELECTION_PROVENANCE_KEYS),
        "records_carrying_any_provenance_key": linked_anywhere,
        "decided_here": [
            "exactly one hypothesis is selected",
            "the selection is not the first recorded with every alternative left open",
            "no alternative is left open",
            "every status is inside the contract's enum",
        ],
        "not_decidable_here": (
            "'linked to the test result' needs a reference from the selection to an observation. "
            "contract.md#7.4's template has no field for one -- no selected_because, no "
            "observation_ref, no test identifier -- so there is nowhere to record the link and "
            "nothing to follow. Zero records in this repository carry any of the "
            f"{len(SELECTION_PROVENANCE_KEYS)} names searched for."
        ),
        "why_prose_is_not_matched_against_measurements": (
            "the selected hypothesis in evidence/FINDING-008-implementer-channel-rate.json reads "
            "'12/12 success at 15s spacing' and the same file's measurements block holds "
            "spaced_15s: {attempts: 12, ok: 12}. Joining those means parsing English into a key "
            "name. That is reading comprehension behind a regular expression, it breaks on the "
            "next artifact, and this gate sets model_judge_in_verdict_path: false. Only exact key "
            "references are followed."
        ),
        "staged": "reported UNVERIFIABLE, not PASS, pending the owner amendment",
        "amendment": "docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md",
    }
    negative_control = {
        "probe": (
            "score a set whose first-recorded hypothesis is selected while every alternative is "
            "still open; a set with two hypotheses marked supported; a set in which nothing is "
            "selected; and a well-formed set whose selection is last and whose alternatives are "
            "all refuted"
        ),
        "why": (
            "'the selection is not by discovery order' is true of a check that never looks at "
            "order, and of one that flags every set. The first three probes are the three shapes "
            "the failure takes and must each be caught; the fourth is the shape that must not be, "
            "or the predicate is refusing everything and its silence on the real corpus means "
            "nothing."
        ),
        "first_found_wins_rejected": first_found,
        "two_survivors_rejected": two_survivors,
        "nothing_selected_rejected": no_selection,
        "well_formed_selection_accepted": not well_formed,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)

    return undecided(
        "UNVERIFIABLE: the decidable half of this assertion holds -- across "
        f"{len(corpus.sets)} recorded set(s) exactly one hypothesis is selected, it is not the "
        "first recorded with the alternatives left open, and no alternative is still open -- but "
        "'selection_linked_to_test_result' cannot be decided. contract.md#7.4's template has no "
        "field in which to record a link from a selection to an observation, and none of the "
        f"{len(SELECTION_PROVENANCE_KEYS)} plausible key names is present on any record "
        f"({linked_anywhere} found). The prose in the selected hypothesis describes an outcome; "
        "matching prose to the measurement block would be reading comprehension in a verdict path "
        "this gate declares model-judge-free. See "
        "docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md.",
        evidence,
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D2_14: dict[tuple[str, str], Check] = {
    ("GATE-D2-14", "A1"): d2_14_a1,
    ("GATE-D2-14", "A2"): d2_14_a2,
    ("GATE-D2-14", "A3"): d2_14_a3,
    ("GATE-D2-14", "A4"): d2_14_a4,
}
