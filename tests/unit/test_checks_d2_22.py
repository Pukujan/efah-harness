"""GATE-D2-22's checks, and the proof that they can fail.

Contract Sections 19.3, 19.4 and 18. A gate check that passes against the real
scheduler tells you very little on its own -- the same green would appear if the
check compared a constant to itself. So every check here is exercised twice:
once against the real ``ContractReviewScheduler``, the real pack and the real
``contract_revalidation_graph``, and once against a subject broken in exactly one
named way.

The broken subjects are the ways this gate is plausibly wrong, not arbitrary
damage:

* a scheduler that fires a review on every material phase (A1) -- the wiring is
  there and the counter is not;
* a scheduler that ignores the pack's declared interval and takes Section 19.3's
  fallback (A1) -- invisible against this pack, which declares the same number,
  unless the check forces the two apart;
* a scheduler whose counter never resets (A1) -- the first review is on time and
  every later one is early;
* a scheduler that matches event triggers loosely, or fires on any string at all
  (A2);
* a review that advances on every outcome, and a Section 19.4 route table with a
  hole in it (A3);
* a scheduler that welcomes an added requirement, and a review that reports no
  additions while its own requirement lists differ (A4).
"""

from __future__ import annotations

from typing import Any

import pytest

from drift import review as drift_review
from drift.review import ContractReview, ContractReviewScheduler, ReviewTrigger
from evaluation import checks_d2_22
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d2_22 import CHECKS_D2_22
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import ContractReviewOutcome, Verdict

GATE_ID = "GATE-D2-22"


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context over the real pack.

    The candidate commit is a stand-in because these tests are about the checks,
    not about the binding; the gate-runner test at the end uses the real HEAD.
    """
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D2_22[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


# --- broken subjects -------------------------------------------------------


class SchedulerFiringEveryPhase(ContractReviewScheduler):
    """Review time is every phase. Satisfies "a review fired after three"."""

    def due_for_phases(self, phases_since_last_review: int) -> ReviewTrigger | None:
        return ReviewTrigger(
            trigger_id="CRT-INTERVAL",
            trigger_type="periodic",
            reason="broken: fires on every phase",
            phases_since_last_review=phases_since_last_review,
        )


class SchedulerNeverFiring(ContractReviewScheduler):
    """The interval never arrives."""

    def due_for_phases(self, phases_since_last_review: int) -> ReviewTrigger | None:
        return None


class SchedulerIgnoringTheDeclaredInterval(ContractReviewScheduler):
    """Reads the pack's event triggers and takes the fallback interval.

    This is the subtle one, and the reason A1 carries a pack-override arm: the
    pack declares 3 and Section 19.3's fallback is 3, so against *this* pack this
    scheduler fires at exactly the phases a correct one does.
    """

    @classmethod
    def from_pack(cls, pack: Any) -> ContractReviewScheduler:
        contract = pack.yaml("contract.yaml")["contract_review"]
        return cls(None, contract.get("event_triggers", []))


class SchedulerThatNeverResets(ContractReviewScheduler):
    """Fires on time once, then on every phase after it."""

    def observe_phase(self, material: bool = True) -> ReviewTrigger | None:
        if material:
            self._counter += 1
        return self.due_for_phases(self._counter)


class SchedulerCountingEveryPhase(ContractReviewScheduler):
    """Counts non-material phases too, so reviews arrive early."""

    def observe_phase(self, material: bool = True) -> ReviewTrigger | None:
        return super().observe_phase(material=True)


class SchedulerFiringOnAnyEvent(ContractReviewScheduler):
    """Every string is an event trigger."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        return ReviewTrigger("CRT-EV-00", "event", "broken: fires on anything", event=event)


class SchedulerNeverFiringOnEvents(ContractReviewScheduler):
    """Declares thirteen triggers and honours none."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        return None


class SchedulerMatchingEventsLoosely(ContractReviewScheduler):
    """Substring and case-insensitive matching -- the plausible sloppy version."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        for index, declared in enumerate(self.event_triggers):
            if declared in event or event.strip().lower() == declared:
                return ReviewTrigger(
                    f"CRT-EV-{index + 1:02d}", "event", "broken: loose match", event=event
                )
        return None


class SchedulerMistypingEventTriggers(ContractReviewScheduler):
    """Fires at the right events and types them as periodic reviews."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        trigger = super().due_for_event(event)
        if trigger is None:
            return None
        return ReviewTrigger(
            trigger.trigger_id, "periodic", trigger.reason, event=trigger.event
        )


class SchedulerThatAlwaysReaffirms(ContractReviewScheduler):
    """Every review comes back clean, whatever it was handed."""

    def review(self, **kwargs: Any) -> ContractReview:
        review = super().review(**kwargs)
        review.outcome = ContractReviewOutcome.CONTRACT_REAFFIRMED
        return review


class SchedulerClaimingEverythingAdvances(ContractReviewScheduler):
    """Section 19.4 with the "only" removed."""

    @staticmethod
    def advances_automatically(outcome: ContractReviewOutcome) -> bool:
        return True


class SchedulerThatWelcomesImprovements(ContractReviewScheduler):
    """Adds the review's optional improvements to scope and reaffirms."""

    def review(self, **kwargs: Any) -> ContractReview:
        review = super().review(**kwargs)
        if review.outcome is ContractReviewOutcome.DRIFT_DETECTED and not review.findings:
            review.outcome = ContractReviewOutcome.CONTRACT_REAFFIRMED
        return review


class ReviewBlindToAdditions(ContractReview):
    """Reports no added requirements however many its lists differ by."""

    @property
    def added_requirements(self) -> list[str]:
        return []


class SchedulerReturningBlindReviews(ContractReviewScheduler):
    def review(self, **kwargs: Any) -> ContractReview:
        review = super().review(**kwargs)
        return ReviewBlindToAdditions(
            review_id=review.review_id,
            trigger=review.trigger,
            outcome=review.outcome,
            findings=list(review.findings),
            requirements_before=list(review.requirements_before),
            requirements_after=list(review.requirements_after),
            evidence=list(review.evidence),
        )


# --- the registry ----------------------------------------------------------


def test_the_registry_covers_every_assertion_the_pack_declares(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D2_22 if gid == GATE_ID}
    assert registered == declared
    assert all(gid == GATE_ID for gid, _ in CHECKS_D2_22)


def test_merging_the_map_would_not_displace_another_gates_check():
    """Registration lives in ``checks.py``; this module only offers the map.

    ``dict.update`` wins silently, so the hazard worth a test is a key in this
    map that already resolves to somebody else's function. Whether the merge has
    happened yet is ``checks.py``'s business and changes over time; a collision
    would be a bug in any state.
    """
    collisions = {
        key for key, check in CHECKS_D2_22.items() if key in CHECKS and CHECKS[key] is not check
    }
    assert not collisions


def test_this_module_does_not_import_checks_at_module_scope():
    """The circular-import guard, stated as a test rather than as a comment.

    ``checks.py`` imports this module to register it. If this module imported
    ``evaluation.checks`` at module scope the pair would be circular, and which
    side broke would depend on import order -- working under the gate runner and
    exploding under pytest.
    """
    source = (checks_d2_22.__file__ or "").strip()
    assert source.endswith("checks_d2_22.py")
    with open(source) as handle:
        lines = [line for line in handle if not line.startswith(" ")]
    module_scope_imports = [
        line for line in lines if line.startswith(("import ", "from ")) and "evaluation.checks" in line
    ]
    assert module_scope_imports == []


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_scheduler(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["pack_declared_interval_material_phases"] == 3
    assert log["scheduler_interval_material_phases"] == 3
    assert log["fired_at_material_phases"] == [3, 6]


def test_a1_proves_the_review_does_not_fire_at_phases_one_and_two(ctx, gate):
    """The silence is the property. Firing every phase would satisfy the words."""
    log = run(ctx, gate, "A1").evidence["gate_execution_log"]
    assert {1, 2}.issubset(set(log["did_not_fire_at_material_phases"]))
    assert 1 not in log["fired_at_material_phases"]
    assert 2 not in log["fired_at_material_phases"]


def test_a1_fails_when_the_scheduler_fires_on_every_phase(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerFiringEveryPhase)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("fired at material phase 1" in f for f in outcome.findings)
    assert any("fired at material phase 2" in f for f in outcome.findings)


def test_a1_fails_when_the_scheduler_never_fires(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerNeverFiring)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("no review fired at material phase 3" in f for f in outcome.findings)


def test_a1_fails_when_from_pack_takes_the_default_instead_of_the_declared_value(
    ctx, gate, monkeypatch
):
    """The arm that stops A1 from measuring Section 19.3's fallback.

    Against this pack the broken scheduler fires at phases 3 and 6, exactly like
    a correct one, because the declared interval and the default are both 3. Only
    the pack-override arm tells them apart.
    """
    monkeypatch.setattr(
        checks_d2_22, "ContractReviewScheduler", SchedulerIgnoringTheDeclaredInterval
    )
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("the declared value is not the one being scheduled on" in f for f in outcome.findings)


def test_a1_fails_when_the_counter_never_resets(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerThatNeverResets)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("the counter did not reset" in f for f in outcome.findings)


def test_a1_fails_when_non_material_phases_advance_the_counter(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerCountingEveryPhase)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any(
        "with two non-material phases before each phase" in f and "material phase 1" in f
        for f in outcome.findings
    )


def test_a1_records_why_the_pack_value_and_the_default_had_to_be_forced_apart(ctx, gate):
    arm = run(ctx, gate, "A1").evidence["gate_execution_log"][
        "the_declared_value_is_read_rather_than_defaulted"
    ]
    assert arm["shim_declares"] == 5
    assert arm["scheduler_interval_material_phases"] == 5
    assert arm["fired_at_material_phases"] == [5]
    assert "never opened the pack" in arm["why"]


def test_a1_negative_controls_are_caught_at_the_early_phases(ctx, gate):
    control = run(ctx, gate, "A1").evidence["negative_control_transcript"]
    assert control["fires_every_phase"]["caught_at_early_phases"] == {1: True, 2: True}
    assert control["never_fires"]["caught_the_missed_review"] is True


# --- A2 --------------------------------------------------------------------


def test_a2_passes_against_the_real_scheduler(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["pack_declared_event_trigger_count"] == 13
    assert "after_walking_skeleton" in log["declared_events_that_fired"]
    assert log["declared_events_that_fired"] == log["pack_declared_event_triggers"]
    assert log["undeclared_events_that_fired"] == []


def test_a2_carries_the_named_event_through_a_real_review(ctx, gate):
    review = run(ctx, gate, "A2").evidence["gate_execution_log"]["review_built_from_the_named_event"]
    assert review["review_ran"] is True
    assert review["trigger"]["event"] == "after_walking_skeleton"
    assert review["trigger"]["trigger_type"] == "event"
    assert review["outcome"] == str(ContractReviewOutcome.CONTRACT_REAFFIRMED)


def test_a2_fails_when_the_scheduler_fires_on_any_string(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerFiringOnAnyEvent)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("because_i_felt_like_it" in f and "fired a review" in f for f in outcome.findings)


def test_a2_fails_when_no_declared_event_fires(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerNeverFiringOnEvents)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("after_walking_skeleton" in f and "no review fired" in f for f in outcome.findings)


def test_a2_fails_on_loose_event_matching(ctx, gate, monkeypatch):
    """The near-miss probes earn their place here.

    A substring match fires at ``after_walking_skeleton_v2``; a case-folding one
    fires at ``AFTER_WALKING_SKELETON``. Both would pass a check that only ever
    probed the thirteen real triggers and one piece of obvious nonsense.
    """
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerMatchingEventsLoosely)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("after_walking_skeleton_v2" in f for f in outcome.findings)
    assert any("AFTER_WALKING_SKELETON" in f for f in outcome.findings)


def test_a2_fails_when_an_event_trigger_is_typed_as_a_periodic_one(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerMistypingEventTriggers)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("not 'event'" in f for f in outcome.findings)


# --- A3 --------------------------------------------------------------------


def test_a3_passes_against_the_real_review_and_graph(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    drift = log["outcome_sweep"][str(ContractReviewOutcome.DRIFT_DETECTED)]
    assert drift["advances_automatically"] is False
    assert drift["remediation_route"] == "scope_drift_remediation"
    assert log["outcomes_that_advanced"] == [str(ContractReviewOutcome.CONTRACT_REAFFIRMED)]
    assert log["runtime_graph"]["typed_blocker_raised"] == ["STALE_CONTRACT_VERSION"]


def test_a3_sweeps_every_section_19_4_outcome(ctx, gate):
    """"An outcome other than CONTRACT_REAFFIRMED" is a claim about all of them."""
    sweep = run(ctx, gate, "A3").evidence["gate_execution_log"]["outcome_sweep"]
    assert set(sweep) == {str(o) for o in ContractReviewOutcome}
    for name, record in sweep.items():
        advancing = name == str(ContractReviewOutcome.CONTRACT_REAFFIRMED)
        assert record["advances_automatically"] is advancing
        assert (record["remediation_route"] is None) is advancing


def test_a3_records_that_the_graph_edge_does_not_stop(ctx, gate):
    """The limit is part of the evidence, not a comment somebody can drop.

    The project graph's ``contract_revalidation -> planning`` edge is
    unconditional. A3 must not imply the graph halts.
    """
    halt = run(ctx, gate, "A3").evidence["gate_execution_log"]["how_the_halt_is_expressed"]
    assert halt["outgoing_edges_from_contract_revalidation"] == ["planning"]
    assert halt["advance_edge_is_conditional"] is False
    assert "does NOT claim the graph stops" in halt["limit"]


def test_a3_fails_when_a_drifted_review_still_advances(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerThatAlwaysReaffirms)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("derived outcome CONTRACT_REAFFIRMED" in f for f in outcome.findings)


def test_a3_fails_when_the_scheduler_says_every_outcome_advances(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d2_22, "ContractReviewScheduler", SchedulerClaimingEverythingAdvances
    )
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("while the scheduler says True" in f for f in outcome.findings)


def test_a3_fails_when_a_halt_names_no_remediation_route(ctx, gate, monkeypatch):
    """A halt with no route is a stall. Section 19.4 requires the route."""
    routes = {
        outcome: route
        for outcome, route in drift_review.REMEDIATION_ROUTE.items()
        if outcome is not ContractReviewOutcome.DRIFT_DETECTED
    }
    monkeypatch.setattr(drift_review, "REMEDIATION_ROUTE", routes)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("names no remediation route" in f for f in outcome.findings)


def test_a3_fails_when_the_drifted_graph_run_writes_no_typed_blocker(ctx, gate, monkeypatch):
    real = checks_d2_22._graph_probe

    def blockerless(repo_root):
        probe = real(repo_root)
        probe["drifted_run"] = {**probe["drifted_run"], "typed_blockers": []}
        return probe

    monkeypatch.setattr(checks_d2_22, "_graph_probe", blockerless)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("nothing typed halts the advance" in f for f in outcome.findings)


def test_a3_fails_when_the_graph_blocks_even_without_drift(ctx, gate, monkeypatch):
    """A graph that always blocks proves nothing about drift."""
    real = checks_d2_22._graph_probe

    def always_blocking(repo_root):
        probe = real(repo_root)
        probe["reaffirmed_run"] = {
            **probe["reaffirmed_run"],
            "typed_blockers": ["STALE_CONTRACT_VERSION"],
        }
        return probe

    monkeypatch.setattr(checks_d2_22, "_graph_probe", always_blocking)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("a graph that always blocks" in f for f in outcome.findings)


def test_a3_negative_control_keeps_the_clean_review_advancing(ctx, gate):
    control = run(ctx, gate, "A3").evidence["negative_control_transcript"]
    assert control["clean_review_still_advances"]["advances_automatically"] is True
    assert control["reaffirming_graph_run"]["typed_blockers"] == []
    assert control["always_advancing_review"]["caught_drift_detected"] is True


# --- A4 --------------------------------------------------------------------


def test_a4_passes_against_the_real_review(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["requirements_before_count"] > 0
    assert log["conformance_review"]["added_requirements"] == []
    assert log["conformance_review"]["scope_expanded"] is False
    assert log["conformance_review"]["outcome"] == str(ContractReviewOutcome.CONTRACT_REAFFIRMED)


def test_a4_forces_drift_when_a_review_adds_an_optional_improvement(ctx, gate):
    control = run(ctx, gate, "A4").evidence["negative_control_transcript"]["improvement_review"]
    assert control["added_requirements"] == ["REQ-OPTIONAL-IMPROVEMENT-001"]
    assert control["outcome"] == str(ContractReviewOutcome.DRIFT_DETECTED)
    assert control["scope_expansion_finding"] == "UNAPPROVED_SCOPE_EXPANSION"
    assert control["advances_automatically"] is False
    assert control["remediation_route"]


def test_a4_fails_when_a_review_may_add_requirements(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerThatWelcomesImprovements)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("reaffirmed the contract and advanced automatically" in f for f in outcome.findings)


def test_a4_fails_when_the_review_reports_no_additions_it_did_make(ctx, gate, monkeypatch):
    """A4 recomputes the diff instead of reading the review's claim about itself."""
    monkeypatch.setattr(checks_d2_22, "ContractReviewScheduler", SchedulerReturningBlindReviews)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("recomputation over its own requirement lists" in f for f in outcome.findings)


def test_a4_records_that_the_scope_diff_is_one_directional(ctx, gate):
    """A dropped requirement is REQUIREMENT_WEAKENING, which this gate does not measure."""
    arm = run(ctx, gate, "A4").evidence["gate_execution_log"]["scope_diff_is_one_directional"]
    assert arm["removal_review"]["added_requirements"] == []
    assert arm["removal_review"]["outcome"] == str(ContractReviewOutcome.CONTRACT_REAFFIRMED)
    assert "REQUIREMENT_WEAKENING" in arm["limit"]


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_emits_the_evidence_the_gate_named(ctx, gate, assertion_id):
    outcome = run(ctx, gate, assertion_id)
    assert set(gate.evidence_required) <= set(outcome.evidence)
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")
    assert binding["pack_manifest_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = run(ctx, gate, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


def test_the_registered_gate_runs_green_with_its_evidence(monkeypatch):
    """The registration entries, exercised end to end through the runner.

    This is what merging :data:`CHECKS_D2_22` into ``CHECKS`` buys: the gate
    reports EXECUTED rather than NOT_YET_EXECUTABLE, and produces every artifact
    its own definition named.
    """
    for key, check in CHECKS_D2_22.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]
    assert result.evidence_missing == []
    assert result.executed_count == len(result.assertions) == 4
