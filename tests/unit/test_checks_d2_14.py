"""GATE-D2-14's four checks, and the proof that the two decidable ones can fail.

Contract Section 18. Every check in :mod:`evaluation.checks_d2_14` is run twice:
once against the real repository, and once against a subject in which the
property the assertion names is false. The second run is what gives the first
its meaning, and each broken subject is broken in exactly one named way.

Two of the four are staged to ``UNVERIFIABLE`` rather than decided, so this
module has an extra obligation the other check suites do not: it must pin the
*reason* for the staging, not just the status. A staged assertion whose reason
nobody tests becomes the next stale ``NOT_EXECUTABLE_REASONS`` entry -- an
inability recorded once and never looked at again, which is the failure mode
:mod:`evaluation.checks_audit_followup` was written to remember. So:

* :func:`test_a3_stages_because_string_inequality_cannot_fire` asserts that the
  inert check really is inert on five restatements of one theory. If somebody
  makes it fire, this test fails and A3's staging must be revisited.
* :func:`test_the_arithmetic_predicate_would_decide_it` asserts the replacement
  predicate separates a discriminating typed set from a non-discriminating one.
  If it stops doing so, the amendment proposal is wrong and this fails first.
* :func:`test_a4_stages_only_the_half_it_cannot_decide` asserts the decidable
  half really is decided, so "UNVERIFIABLE" cannot quietly widen into "we did
  not look".

The failure of every negative arm is asserted by *message*, not merely by
status: a check that fails for the wrong reason has stopped measuring its
assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

from evaluation import checks_d2_14 as d2_14
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d2_14 import (
    CHECKS_D2_14,
    Corpus,
    HypothesisSet,
    TypedObservation,
    pairwise_discriminating,
    predictions_are_incompatible,
)
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict

GATE = "GATE-D2-14"

#: The three artifacts the gate names, owed by every one of its four assertions.
REQUIRED_EVIDENCE = (
    "gate_execution_log",
    "negative_control_transcript",
    "artifact_hashes_and_commit_binding",
)


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    return GateContext(binding=CandidateBinding(commit_sha="d" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gates: dict[str, GateSpec], assertion_id: str) -> Any:
    gate = gates[GATE]
    return CHECKS_D2_14[(GATE, assertion_id)](ctx, gate, assertion(gate, assertion_id))


def findings_mentioning(outcome: Any, fragment: str) -> list[str]:
    return [f for f in outcome.findings if fragment in f]


def corpus_of(*sets: HypothesisSet) -> Corpus:
    return Corpus(sets, files_scanned=len(sets) or 1, unreadable=(), rejected=())


# --- the registry and the circular-import rule -----------------------------


def test_the_registry_holds_all_four_assertions(gates):
    assert set(CHECKS_D2_14) == {(GATE, aid) for aid in ("A1", "A2", "A3", "A4")}
    declared = {a.assertion_id for a in gates[GATE].assertions}
    assert {aid for _, aid in CHECKS_D2_14} == declared


def test_the_gate_forbids_a_model_in_its_verdict_path(gates):
    """``model_judge_in_verdict_path: false``, and nothing here calls a model.

    Worth pinning rather than assuming: the two staged assertions are staged
    *because* the honest alternative would have been reading comprehension, and
    reading comprehension in this verdict path is the thing the flag forbids.
    """
    assert gates[GATE].model_judge_in_verdict_path is False


def test_this_module_never_imports_checks_at_module_scope():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(d2_14.__file__).read_text())
    offenders = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "evaluation.checks"
    ]
    assert offenders == [], f"module-scope import of {offenders} closes the registration cycle"


# --- A1: at least two hypotheses -------------------------------------------


def test_a1_passes_against_the_recorded_corpus(ctx, gates):
    outcome = run(ctx, gates, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["minimum_from_the_pack"] == 2
    assert log["corpus"]["hypothesis_sets_found"] >= 1
    assert log["corpus"]["structured_files_scanned"] > 50
    assert all(entry["meets_the_minimum"] for entry in log["per_set"])


def test_a1_records_that_the_denominator_is_unknowable(ctx, gates):
    """The coverage limit is stated in the evidence, not left for a reader to infer."""
    log = run(ctx, gates, "A1").evidence["gate_execution_log"]
    assert "no module in src/ emits an efah.hypothesis record" in log["what_this_does_not_decide"]


def test_a1_fails_on_an_empty_corpus(ctx, gates, monkeypatch):
    """'Every recorded set has at least two' is true of a repository with none."""
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of())
    outcome = run(ctx, gates, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "statement about an empty search")


def test_a1_fails_on_a_single_hypothesis_set(ctx, gates, monkeypatch):
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of(d2_14._single_hypothesis_set()))
    outcome = run(ctx, gates, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "the first plausible fix wearing a form")


def test_a1_fails_when_the_count_predicate_never_counts(ctx, gates, monkeypatch):
    """A predicate that accepts everything reports 'all sets meet the minimum' for free."""
    monkeypatch.setattr(d2_14, "count_findings", lambda subject, minimum: [])
    outcome = run(ctx, gates, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "negative control did not fire")


def test_a1_fails_when_the_gate_and_the_policy_disagree_on_the_threshold(ctx, gates, monkeypatch):
    gate = gates[GATE]
    spec = assertion(gate, "A1")
    outcome = CHECKS_D2_14[(GATE, "A1")](
        ctx, gate, type(spec)(**{**vars(spec), "expected": "count >= 5"})
    )
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "a threshold the gate does not state")


# --- A2: eight fields, present and non-empty -------------------------------


def test_a2_fails_on_the_real_records_and_names_every_absent_field(ctx, gates):
    """The repository does not satisfy Section 7.4, and this is what is missing.

    Not staged. Unlike A3 and A4 this is decidably *false* rather than
    undecidable: six records, four keys each, and the declared names are simply
    not there. Reporting it as UNVERIFIABLE would be recording an inability the
    check does not have.
    """
    outcome = run(ctx, gates, "A2")
    assert outcome.status is AssertionStatus.FAIL
    for field in ("hypothesis_id", "supporting_evidence", "discriminating_tests", "confidence"):
        assert findings_mentioning(outcome, f"required field {field!r} is absent")
    assert findings_mentioning(outcome, "expected_observations")
    assert findings_mentioning(outcome, "'supported_then_refined' is outside the contract's enum")


def test_a2_reports_near_misses_without_accepting_them(ctx, gates):
    """``id`` is not ``hypothesis_id``; the finding names the near miss and still fails."""
    records = run(ctx, gates, "A2").evidence["gate_execution_log"]["per_set"][0]["records"]
    first = records[0]
    assert first["keys_that_nearly_match_a_declared_field"] == {"id": ["hypothesis_id"]}
    assert "hypothesis_id" in first["declared_fields_absent"]
    later = next(r for r in records if "discriminating_test" in r["keys_that_nearly_match_a_declared_field"])
    assert later["keys_that_nearly_match_a_declared_field"]["discriminating_test"] == [
        "discriminating_tests"
    ]


def test_a2_does_not_call_id_a_near_miss_of_supporting_evidence():
    """Substring matching would; a finding that names the wrong culprit is worse than none."""
    assert d2_14._is_near_miss("id", "hypothesis_id")
    assert d2_14._is_near_miss("discriminating_test", "discriminating_tests")
    assert not d2_14._is_near_miss("id", "supporting_evidence")
    assert not d2_14._is_near_miss("id", "contradicting_evidence")
    assert not d2_14._is_near_miss("id", "confidence")


def test_a2_rejects_a_verbatim_copy_of_the_contracts_own_template(ctx, gates):
    """Trap 1, executed. The template has eight keys and proves nothing.

    This is the assertion that makes A2 more than a key count. If this control
    ever stops firing, ``all_eight_fields_present`` has become satisfiable by
    copy-paste and the check is decorative.
    """
    control = run(ctx, gates, "A2").evidence["negative_control_transcript"]
    rejected = control["verbatim_template_rejected_for_emptiness"]
    assert rejected, "a verbatim copy of contract.md#7.4's template was accepted"
    for field in d2_14.LOAD_BEARING_LISTS:
        assert any(f"required field {field!r} is present but empty" in f for f in rejected)
    assert control["populated_pair_accepted"] is True


def test_a2_control_template_is_the_owners_document_not_a_restatement(ctx):
    """The control is parsed out of ``contract.md`` so it cannot drift from it."""
    template = d2_14._contract_template(ctx.repo_root)
    assert sorted(template) == sorted(d2_14._declared_fields(ctx))
    assert template["supporting_evidence"] == []
    assert template["expected_observations"] == []
    enums = d2_14._template_enums(template)
    assert enums["status"] == ("open", "supported", "refuted", "inconclusive")
    assert enums["confidence"] == ("unknown", "low", "medium", "high")


def test_a2_passes_a_fully_populated_corpus(ctx, gates, monkeypatch):
    """The predicate is selective: it accepts records that really carry the eight."""
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of(d2_14._well_formed_set()))
    outcome = run(ctx, gates, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    assert "A3 is staged UNVERIFIABLE" in outcome.evidence["gate_execution_log"][
        "what_a_green_here_would_not_mean"
    ]


def test_a2_fails_when_a_populated_record_is_emptied(ctx, gates, monkeypatch):
    """The property made false in the data, not in the predicate."""
    hollow = d2_14._populated_record("H-001", "a claim") | {"expected_observations": []}
    subject = HypothesisSet(
        "<test>", ".hollowed", (hollow, d2_14._populated_record("H-002", "b", "supported"))
    )
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of(subject))
    outcome = run(ctx, gates, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "'expected_observations' is present but empty")


def test_a2_fails_when_the_emptiness_check_is_removed(ctx, gates, monkeypatch):
    """Presence-only is the trap; with it, the template control cannot fire."""
    monkeypatch.setattr(d2_14, "is_placeholder", lambda value: False)
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of(d2_14._well_formed_set()))
    outcome = run(ctx, gates, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "inert by construction")


def test_a2_records_that_nothing_enforces_the_compiled_schema(ctx, gates):
    """Evidence, not a finding -- the assertion is about records, not validators."""
    report = run(ctx, gates, "A2").evidence["gate_execution_log"]["schema_declared_but_unenforced"]
    assert report["src_files_that_mention_the_schema_id"] == ["src/contracts/compiler.py"]
    assert "validated by nothing" in report["note"]


# --- A3: staged, and the reason pinned -------------------------------------


def test_a3_is_unverifiable_and_says_why(ctx, gates):
    outcome = run(ctx, gates, "A3")
    assert outcome.status is AssertionStatus.UNVERIFIABLE
    assert "cannot be honestly decided" in outcome.note
    assert "string inequality" in outcome.note
    assert "PROPOSED-AMENDMENT-001" in outcome.note


def test_a3_stages_because_string_inequality_cannot_fire(ctx, gates):
    """The demonstration that the naive check is inert, pinned as a fact.

    Five differently-worded statements of one theory. A string-inequality
    distinctness check reports every pair distinct, so it can never fail on the
    input it most needs to fail on. This is the whole reason A3 does not ship
    one, and if this assertion ever inverts, that reasoning must be revisited
    rather than silently outlived.
    """
    control = run(ctx, gates, "A3").evidence["negative_control_transcript"]
    inert = control["inert_check_that_cannot_fire"]
    assert len(inert["input"]) == 5
    assert len(set(inert["input"])) == 5, "the restatements must be distinct strings"
    assert inert["string_inequality_reports_all_pairs_distinct"] is True
    assert len(inert["pairs"]) == 10
    assert all(pair["strings_differ"] for pair in inert["pairs"])


def test_a3_records_zero_typed_expected_observations(ctx, gates):
    log = run(ctx, gates, "A3").evidence["gate_execution_log"]
    assert log["typed_expected_observations_in_the_whole_repository"] == 0
    assert "not decidable" in log["the_three_questions"]["executed"]
    assert "A2's finding" in log["the_three_questions"]["present"]


def test_a3_measures_the_decidable_parts_rather_than_skipping_them(ctx, gates):
    """Staged is not the same as unexamined."""
    records = run(ctx, gates, "A3").evidence["gate_execution_log"]["per_set"][0]["records"]
    assert len(records) == 6
    assert not any(r["declared_field_present"] for r in records)
    assert not any(r["runnable_reference_keys_found"] for r in records)
    assert not any(r["expected_observations_present"] for r in records)
    assert [r["near_miss_keys"] for r in records].count(["discriminating_test"]) == 5


def test_a3_fails_if_the_inert_demonstration_stops_demonstrating(ctx, gates, monkeypatch):
    monkeypatch.setattr(
        d2_14,
        "string_distinct_predictions",
        lambda subject: {"compared": [], "pairs": [], "all_pairs_reported_distinct": False},
    )
    outcome = run(ctx, gates, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "no longer shows what it claims to show")


def test_a3_fails_if_the_replacement_predicate_is_broken(ctx, gates, monkeypatch):
    monkeypatch.setattr(d2_14, "pairwise_discriminating", lambda predictions: (False, []))
    outcome = run(ctx, gates, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "failed its own worked example")


# --- the arithmetic the amendment buys -------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "incompatible"),
    [
        (("==", 0.0), (">=", 1.0), True),
        ((">", 0.9), (">=", 0.95), False),
        (("<", 1.0), (">=", 1.0), True),
        (("<=", 1.0), (">=", 1.0), False),  # they meet at exactly 1.0
        (("<=", 1.0), (">", 1.0), True),
        (("==", 1.0), ("!=", 1.0), True),
        (("==", 1.0), ("!=", 2.0), False),
        ((">=", 0.0), ("!=", 0.5), False),  # the reals are dense
    ],
)
def test_predictions_are_incompatible_is_interval_arithmetic(left, right, incompatible):
    a = TypedObservation("x", left[0], left[1], "ratio")
    b = TypedObservation("x", right[0], right[1], "ratio")
    hit, why = predictions_are_incompatible(a, b)
    assert hit is incompatible, why
    assert why


def test_different_observables_never_contradict_each_other():
    hit, why = predictions_are_incompatible(
        TypedObservation("latency_ms", "<", 100, "ms"),
        TypedObservation("success_rate", ">", 0.99, "ratio"),
    )
    assert hit is False
    assert "different observables" in why


def test_a_comparison_across_units_is_not_a_comparison():
    hit, why = predictions_are_incompatible(
        TypedObservation("latency", "<", 100, "ms"),
        TypedObservation("latency", ">", 1, "s"),
    )
    assert hit is False
    assert "not a comparison" in why


def test_the_arithmetic_predicate_would_decide_it(ctx, gates):
    """The claim in PROPOSED-AMENDMENT-001 §4, run rather than asserted."""
    control = run(ctx, gates, "A3").evidence["negative_control_transcript"]
    typed = control["arithmetic_predicate_under_the_proposed_typed_shape"]
    assert typed["discriminating_set"]["pairwise_discriminating"] is True
    assert typed["non_discriminating_set"]["pairwise_discriminating"] is False
    why = typed["discriminating_set"]["pairs"][0]["why"][0]
    assert "no common satisfying measurement" in why


def test_pairwise_discriminating_needs_every_pair_not_merely_one():
    """Three hypotheses, two of which predict the same thing, is not a discriminating set."""
    predictions = {
        "A": [TypedObservation("rate", "==", 0.0, "ratio")],
        "B": [TypedObservation("rate", ">=", 1.0, "ratio")],
        "C": [TypedObservation("rate", ">=", 1.0, "ratio")],
    }
    decided, pairs = pairwise_discriminating(predictions)
    assert decided is False
    undecided_pair = next(p for p in pairs if not p["discriminated"])
    assert {undecided_pair["a"], undecided_pair["b"]} == {"B", "C"}
    assert "no outcome of the test separates them" in undecided_pair["why"][0]


def test_pairwise_discriminating_is_false_for_a_single_hypothesis():
    """One hypothesis has no pairs, and 'all pairs discriminate' must not be vacuous."""
    decided, pairs = pairwise_discriminating({"A": [TypedObservation("r", "==", 1.0, "ratio")]})
    assert pairs == []
    assert decided is False


# --- A4: staged, and only for the half it cannot decide --------------------


def test_a4_is_unverifiable_and_says_which_half(ctx, gates):
    outcome = run(ctx, gates, "A4")
    assert outcome.status is AssertionStatus.UNVERIFIABLE
    assert "the decidable half of this assertion holds" in outcome.note
    assert "no field in which to record a link" in outcome.note
    assert "PROPOSED-AMENDMENT-001" in outcome.note


def test_a4_stages_only_the_half_it_cannot_decide(ctx, gates):
    """The decidable half really is decided, so UNVERIFIABLE cannot widen into 'we did not look'."""
    log = run(ctx, gates, "A4").evidence["gate_execution_log"]
    report = log["per_set"][0]
    assert report["selected"] == ["H-006"]
    assert report["selection_is_the_first_recorded"] is False
    assert report["alternatives_left_open"] == []
    assert report["statuses_outside_the_contract_enum"] == ["supported_then_refined"]
    assert log["records_carrying_any_provenance_key"] == 0
    assert len(log["provenance_keys_searched_for"]) == len(d2_14.SELECTION_PROVENANCE_KEYS)


def test_a4_refuses_to_match_prose_against_the_measurement_block(ctx, gates):
    """The judgement that keeps a model out of the verdict path, recorded as evidence."""
    log = run(ctx, gates, "A4").evidence["gate_execution_log"]
    why = log["why_prose_is_not_matched_against_measurements"]
    assert "12/12 success at 15s spacing" in why
    assert "model_judge_in_verdict_path: false" in why


def test_a4_catches_a_selection_that_was_merely_first(ctx, gates):
    control = run(ctx, gates, "A4").evidence["negative_control_transcript"]
    assert any("found first" in f for f in control["first_found_wins_rejected"])
    assert control["two_survivors_rejected"]
    assert control["nothing_selected_rejected"]
    assert control["well_formed_selection_accepted"] is True


def test_a4_fails_on_a_corpus_selected_by_discovery_order(ctx, gates, monkeypatch):
    """The property made false in the data: first recorded, nothing else disposed of."""
    monkeypatch.setattr(
        d2_14, "_corpus", lambda repo_root: corpus_of(d2_14._first_found_wins_set())
    )
    outcome = run(ctx, gates, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "which is what Section 7.4 forbids")


def test_a4_fails_when_two_hypotheses_survive(ctx, gates, monkeypatch):
    monkeypatch.setattr(d2_14, "_corpus", lambda repo_root: corpus_of(d2_14._two_survivors_set()))
    outcome = run(ctx, gates, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "leaves two survivors did not discriminate")


def test_a4_fails_when_the_selection_predicate_accepts_everything(ctx, gates, monkeypatch):
    monkeypatch.setattr(d2_14, "selection_findings", lambda subject, enums: ([], {}))
    outcome = run(ctx, gates, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "negative control did not fire")


def test_a4_fails_when_the_selected_status_is_not_in_the_contracts_enum(ctx, gates, monkeypatch):
    monkeypatch.setattr(d2_14, "SELECTED_STATUS", "confirmed")
    outcome = run(ctx, gates, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "a name the contract does not use")


# --- evidence, controls and the runner -------------------------------------


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_emits_the_three_artifacts_the_gate_names(ctx, gates, assertion_id):
    outcome = run(ctx, gates, assertion_id)
    assert set(REQUIRED_EVIDENCE) <= set(outcome.evidence)
    assert set(REQUIRED_EVIDENCE) <= set(gates[GATE].evidence_required)


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gates, assertion_id):
    control = run(ctx, gates, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_binds_its_transcript_to_the_candidate_commit(ctx, gates, assertion_id):
    binding = run(ctx, gates, assertion_id).evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


def test_no_assertion_of_this_gate_carries_an_excuse_for_not_running():
    """An assertion cannot both have a check and a reason for having none."""
    from evaluation.checks import NOT_EXECUTABLE_REASONS

    assert not [key for key in NOT_EXECUTABLE_REASONS if key[0] == GATE]


def test_the_gate_runs_end_to_end_through_the_runner():
    """All four execute; none is NOT_IMPLEMENTED; the verdict is the honest one.

    A2 fails on the real records, so the gate is red rather than unverifiable --
    which is the correct report for a repository whose only recorded hypotheses
    carry four of the eight fields Section 7.4 requires. The two staged
    assertions are UNVERIFIABLE with a stated reason, not PASS.
    """
    result = GateRunner().run([GATE]).results[0]
    statuses = {a.assertion_id: a.status for a in result.assertions}

    assert AssertionStatus.NOT_IMPLEMENTED not in statuses.values()
    assert statuses["A1"] is AssertionStatus.PASS
    assert statuses["A2"] is AssertionStatus.FAIL
    assert statuses["A3"] is AssertionStatus.UNVERIFIABLE
    assert statuses["A4"] is AssertionStatus.UNVERIFIABLE
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.FAIL
    #: Staged deliberately, with the reason each one is staged. **Delete this
    #: carve-out when the amendment lands** -- left undated it becomes the next
    #: stale "not executable" entry, which is the failure that
    #: ``tests/unit/test_checks_audit_followup.py`` exists to remember.
    staged = {
        "A3": "expected_observations is free text; distinctness is not decidable",
        "A4": "no field exists in which to record a link from a selection to a test result",
    }
    for assertion_id in staged:
        outcome = next(a for a in result.assertions if a.assertion_id == assertion_id)
        assert "PROPOSED-AMENDMENT-001" in " ".join(outcome.findings)


def test_the_gate_is_registered_in_the_shared_map():
    for key, check in CHECKS_D2_14.items():
        assert CHECKS[key] is check
