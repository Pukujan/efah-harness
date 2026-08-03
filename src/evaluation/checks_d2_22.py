"""GATE-D2-22 — periodic and event-triggered contract review.

Contract Sections 19.3 and 19.4. The gate's four assertions are executed here
against the real scheduler (:mod:`drift.review`), the real project pack, the
real compiled requirement set, and -- for A3 -- the real
``contract_revalidation_graph``. Nothing is mocked: the only substituted objects
are the deliberately broken schedulers and reviews the negative controls need,
and none of them is ever the subject of a positive arm.

This lives outside :mod:`evaluation.checks` because it is a self-contained set
with its own probe machinery; :data:`CHECKS_D2_22` is what the registry merges.

Three things shaped these checks, because each is a way a green here could mean
nothing.

* **The pack's interval and Section 19.3's fallback are the same number.**
  ``project.yaml`` declares ``contract_review_interval_phases: 3`` and
  :data:`~drift.review.DEFAULT_INTERVAL_MATERIAL_PHASES` is also 3, so a
  scheduler that never opened the pack would fire at exactly the same phases as
  one that read it. A1 therefore re-runs ``from_pack`` over a shim declaring a
  *different* interval and requires the firing pattern to move. Without that
  arm, "fires at the configured interval" would be a claim about the fallback.
* **"Fires" only means something against "does not fire".** A scheduler that
  returns a trigger on every phase satisfies "a review fired after three
  material phases", and a scheduler that returns a trigger for every string
  satisfies "a review fired at ``after_walking_skeleton``". A1 and A2 therefore
  assert the silences too -- no periodic fire at phases 1 and 2, no event fire
  for six undeclared near misses -- and run the same predicate over broken
  schedulers that must be caught.
* **A3's halt is a state halt, not a stopped edge.** The project graph's
  ``contract_revalidation -> planning`` edge is unconditional. This check
  measures that and says so. What halts is state: ``advances_automatically`` is
  False, a typed remediation route is named, and the revalidation graph writes a
  typed blocker that the control-plane projection surfaces. The evidence records
  that limit rather than letting a reader infer the graph stops when it does not.
"""

from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contracts.compiler import compile_pack
from drift.review import (
    ADVANCING_OUTCOME,
    DEFAULT_INTERVAL_MATERIAL_PHASES,
    REMEDIATION_ROUTE,
    TERMINAL_ROUTE,
    ContractReview,
    ContractReviewScheduler,
    ReviewTrigger,
)
from evaluation.gate_spec import AssertionSpec, GateSpec
from governance.envelope import content_hash
from governance.states import ContractReviewOutcome, DriftFinding
from integrations.pack import ProjectPack, load_pack

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; ``ok`` and ``bad`` are the only runtime needs, and resolving them
# on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


#: Section 19.3's pack field, named once so a rename surfaces as a finding
#: rather than as a silently defaulted interval.
_PACK_INTERVAL_FIELD = "contract_review_interval_phases"

#: An interval that is neither the pack's declared value nor Section 19.3's
#: fallback, used to prove ``from_pack`` reads the pack (A1).
_OVERRIDE_INTERVAL = 5

#: The event the gate's A2 claim names in prose.
_NAMED_EVENT = "after_walking_skeleton"

#: Events the pack does not declare. Each is a near miss on purpose -- prefix,
#: suffix, case and whitespace variants of a real trigger, plus one invented
#: event and the empty string -- so a loose match is caught as well as a
#: scheduler that fires on anything.
_UNDECLARED_EVENTS: tuple[str, ...] = (
    "because_i_felt_like_it",
    "after_walking_skeleton_v2",
    "walking_skeleton",
    "AFTER_WALKING_SKELETON",
    " after_walking_skeleton",
    "",
)

#: The requirement a review must never invent. Section 19.4: review is
#: conformance checking, "not an invitation to add optional improvements".
_OPTIONAL_IMPROVEMENT = "REQ-OPTIONAL-IMPROVEMENT-001"


# ===========================================================================
# Shared subjects
# ===========================================================================


@functools.lru_cache(maxsize=4)
def _pack(repo_root: Path) -> ProjectPack:
    return load_pack(repo_root / "project-pack")


@functools.lru_cache(maxsize=4)
def _requirement_ids(repo_root: Path) -> tuple[str, ...]:
    """The project's real Requirement ids, compiled from the pack.

    A4 diffs a requirement set across a review. Diffing two invented ids would
    prove the diff works on two invented ids; diffing the project's own
    requirements is the subject the assertion is about.
    """
    project = compile_pack(_pack(repo_root), repo_root=repo_root)
    return tuple(sorted(project.graph.nodes_of_kind("Requirement")))


class _PackWithDeclaredInterval:
    """The real pack with one declared field rewritten, for A1's pack-read arm.

    ``ContractReviewScheduler.from_pack`` reads exactly two files through
    ``pack.yaml``. This shim serves deep copies, so nothing the real pack holds
    is mutated and the pack on disk -- owner data, hash-locked -- is never
    touched.
    """

    def __init__(self, pack: ProjectPack, interval: int) -> None:
        self._pack = pack
        self._interval = interval

    def yaml(self, name: str) -> dict[str, Any]:
        parsed = copy.deepcopy(self._pack.yaml(name))
        if name == "project.yaml":
            parsed["project"][_PACK_INTERVAL_FIELD] = self._interval
        return parsed


class _SchedulerThatFiresEveryPhase(ContractReviewScheduler):
    """The broken scheduler A1 must not accept: every phase is review time.

    This is the plausible half-implementation -- the trigger is wired, the
    counter is not -- and it satisfies "a review fired after three material
    phases" perfectly.
    """

    def due_for_phases(self, phases_since_last_review: int) -> ReviewTrigger | None:
        return ReviewTrigger(
            trigger_id="CRT-INTERVAL",
            trigger_type="periodic",
            reason="negative control: fires on every material phase",
            phases_since_last_review=phases_since_last_review,
        )


class _SchedulerThatNeverFires(ContractReviewScheduler):
    """The other way to be wrong: the interval never arrives."""

    def due_for_phases(self, phases_since_last_review: int) -> ReviewTrigger | None:
        return None


class _SchedulerThatFiresOnAnyEvent(ContractReviewScheduler):
    """Treats every string as one of the contract's event triggers."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        return ReviewTrigger(
            trigger_id="CRT-EV-00",
            trigger_type="event",
            reason="negative control: fires on any event",
            event=event,
        )


class _SchedulerThatNeverFiresOnEvents(ContractReviewScheduler):
    """Declares the triggers and honours none of them."""

    def due_for_event(self, event: str) -> ReviewTrigger | None:
        return None


class _ReviewThatAlwaysAdvances(ContractReview):
    """A review that advances automatically whatever it found (A3's control)."""

    @property
    def advances_automatically(self) -> bool:
        return True


class _ReviewIgnoringAddedRequirements(ContractReview):
    """Reports no added requirements however many it added (A4's control)."""

    @property
    def added_requirements(self) -> list[str]:
        return []


def _trigger_record(trigger: ReviewTrigger | None) -> dict[str, Any]:
    if trigger is None:
        return {"fired": False}
    return {"fired": True, **trigger.as_body()}


def _review_record(review: ContractReview) -> dict[str, Any]:
    body = review.as_body()
    return {
        "review_id": body["review_id"],
        "outcome": body["outcome"],
        "advances_automatically": body["advances_automatically"],
        "remediation_route": body["remediation_route"],
        "terminal_state": body["terminal_state"],
        "findings": body["findings"],
        "added_requirements": body["added_requirements"],
        "scope_expanded": body["scope_expanded"],
        "scope_expansion_finding": body["scope_expansion_finding"],
        "trigger": body["trigger"],
    }


def _binding_evidence(
    ctx: GateContext, execution_log: dict[str, Any], negative_control: dict[str, Any]
) -> dict[str, Any]:
    return {
        "candidate_commit": ctx.binding.commit_sha,
        "contract_version": ctx.binding.contract_version,
        "pack_manifest_hash": _pack(ctx.repo_root).manifest_hash,
        "transcript_hash": content_hash(
            {"execution": execution_log, "negative_control": negative_control}
        ),
    }


# ===========================================================================
# A1 — a review fires after contract_review_interval_phases material phases
# ===========================================================================


def _observe_phases(
    scheduler: ContractReviewScheduler, count: int, *, non_material_between: int = 0
) -> list[dict[str, Any]]:
    """Drive ``count`` material phases through the scheduler, recording each.

    ``non_material_between`` inserts non-material phases before each material
    one. Section 19.3 counts *material* phases, so a scheduler whose counter
    moves on any phase at all is scheduling on something the contract does not
    name; the record carries whether one of those fired.
    """
    observed: list[dict[str, Any]] = []
    for phase in range(1, count + 1):
        non_material_fired = [
            scheduler.observe_phase(material=False) is not None for _ in range(non_material_between)
        ]
        trigger = scheduler.observe_phase()
        observed.append(
            {
                "material_phase": phase,
                "fired": trigger is not None,
                "trigger_id": trigger.trigger_id if trigger else None,
                "trigger_type": trigger.trigger_type if trigger else None,
                "phases_since_last_review": trigger.phases_since_last_review if trigger else None,
                "reason": trigger.reason if trigger else None,
                "non_material_phases_before_it": len(non_material_fired),
                "a_non_material_phase_fired": any(non_material_fired),
            }
        )
    return observed


def _interval_findings(observed: list[dict[str, Any]], interval: int, label: str) -> list[str]:
    """The predicate behind A1, run over whatever phase log it is handed.

    Both arms of A1 use this one function -- the positive arm over the real
    scheduler, the controls over broken ones -- because a control exercising a
    different predicate would prove nothing about the verdict. It asserts the
    silences as well as the fires: firing early is as wrong as never firing, and
    a trigger still counting every phase since the run began means the counter
    never reset.
    """
    findings: list[str] = []
    for record in observed:
        phase = record["material_phase"]
        due = phase % interval == 0
        if record["fired"] and not due:
            findings.append(
                f"{label}: a review fired at material phase {phase} with an interval of {interval}; "
                "a scheduler that fires before its interval is not scheduling on that interval"
            )
        if due and not record["fired"]:
            findings.append(
                f"{label}: no review fired at material phase {phase}, a multiple of the interval "
                f"{interval}"
            )
        if record["fired"] and due:
            if record["trigger_type"] != "periodic":
                findings.append(
                    f"{label}: the trigger at material phase {phase} is typed "
                    f"{record['trigger_type']!r}, not 'periodic'"
                )
            if record["phases_since_last_review"] != interval:
                findings.append(
                    f"{label}: the trigger at material phase {phase} counts "
                    f"{record['phases_since_last_review']} phases since the last review rather than "
                    f"{interval}; the counter did not reset, so the next review is not another "
                    "interval away"
                )
        if record["a_non_material_phase_fired"]:
            findings.append(
                f"{label}: a non-material phase fired a review before material phase {phase}; "
                "Section 19.3 counts material phases"
            )
    return findings


def d2_22_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``phase_counter_probe`` -> ``review_fired_at_interval``.

    The scheduler is built by ``from_pack`` and driven through two full
    intervals plus one phase, so the check sees both fires and the reset between
    them. Three arms make the green mean something:

    * the silences -- nothing fires at phases 1 and 2, or at 4 and 5;
    * non-material phases interleaved, which must not advance the counter;
    * a pack shim declaring a different interval, because the pack's 3 and
      Section 19.3's fallback 3 are otherwise indistinguishable.
    """
    pack = _pack(ctx.repo_root)
    declared = pack.yaml("project.yaml")["project"].get(_PACK_INTERVAL_FIELD)
    scheduler = ContractReviewScheduler.from_pack(pack)
    interval = scheduler.interval_material_phases

    findings: list[str] = []
    if declared is None:
        findings.append(
            f"project.yaml declares no {_PACK_INTERVAL_FIELD}; Section 19.3 requires the field and "
            f"the scheduler fell back to {DEFAULT_INTERVAL_MATERIAL_PHASES} material phases"
        )
    elif int(declared) != interval:
        findings.append(
            f"the pack declares {_PACK_INTERVAL_FIELD}={declared!r} while the scheduler schedules "
            f"on {interval}"
        )

    observed = _observe_phases(scheduler, 2 * interval + 1)
    findings.extend(_interval_findings(observed, interval, "pack interval"))

    interleaved = _observe_phases(
        ContractReviewScheduler.from_pack(pack), interval + 1, non_material_between=2
    )
    findings.extend(
        _interval_findings(interleaved, interval, "with two non-material phases before each phase")
    )

    override = ContractReviewScheduler.from_pack(_PackWithDeclaredInterval(pack, _OVERRIDE_INTERVAL))
    override_interval = override.interval_material_phases
    override_observed = _observe_phases(override, _OVERRIDE_INTERVAL + 1)
    if override_interval != _OVERRIDE_INTERVAL:
        findings.append(
            f"from_pack scheduled on {override_interval} for a pack declaring "
            f"{_PACK_INTERVAL_FIELD}={_OVERRIDE_INTERVAL}; the declared value is not the one being "
            "scheduled on"
        )
    else:
        findings.extend(
            _interval_findings(
                override_observed, _OVERRIDE_INTERVAL, f"pack-declared interval {_OVERRIDE_INTERVAL}"
            )
        )

    # Negative controls: the same predicate over two broken schedulers.
    eager_observed = _observe_phases(_SchedulerThatFiresEveryPhase.from_pack(pack), 2 * interval + 1)
    eager_findings = _interval_findings(
        eager_observed, interval, "negative control: fires every phase"
    )
    early_phases = list(range(1, interval))
    early_caught = {
        phase: any(f"material phase {phase} with an interval" in f for f in eager_findings)
        for phase in early_phases
    }

    deaf_observed = _observe_phases(_SchedulerThatNeverFires.from_pack(pack), 2 * interval + 1)
    deaf_findings = _interval_findings(deaf_observed, interval, "negative control: never fires")
    deaf_caught = any(f"no review fired at material phase {interval}" in f for f in deaf_findings)

    missed_early = sorted(phase for phase, caught in early_caught.items() if not caught)
    if missed_early:
        findings.append(
            f"negative control did not fire for material phases {missed_early}: a scheduler that "
            "fires on every phase was not distinguished from one firing on the interval"
        )
    if not deaf_caught:
        findings.append(
            "negative control did not fire: a scheduler that never fires was not reported as "
            f"missing the review due at material phase {interval}"
        )

    fired_at = [r["material_phase"] for r in observed if r["fired"]]
    silent_at = [r["material_phase"] for r in observed if not r["fired"]]

    execution_log = {
        "check": a.method or "phase_counter_probe",
        "expected": a.expected,
        "contract_ref": "contract.md#19.3",
        "pack_declared_interval_material_phases": declared,
        "section_19_3_default_when_the_field_is_omitted": DEFAULT_INTERVAL_MATERIAL_PHASES,
        "scheduler_interval_material_phases": interval,
        "material_phases_observed": observed,
        "fired_at_material_phases": fired_at,
        "did_not_fire_at_material_phases": silent_at,
        "non_material_phases_do_not_advance_the_counter": {
            "probe": "two non-material phases before each material phase",
            "material_phases_observed": interleaved,
            "fired_at_material_phases": [r["material_phase"] for r in interleaved if r["fired"]],
            "any_non_material_phase_fired": any(
                r["a_non_material_phase_fired"] for r in interleaved
            ),
        },
        "the_declared_value_is_read_rather_than_defaulted": {
            "why": (
                f"project.yaml declares {declared!r} and Section 19.3's fallback is "
                f"{DEFAULT_INTERVAL_MATERIAL_PHASES}. They are the same number, so a scheduler that "
                "never opened the pack would fire at exactly the same phases. This arm re-runs "
                f"from_pack over a shim declaring {_OVERRIDE_INTERVAL} and requires the firing "
                "pattern to move."
            ),
            "shim_declares": _OVERRIDE_INTERVAL,
            "scheduler_interval_material_phases": override_interval,
            "material_phases_observed": override_observed,
            "fired_at_material_phases": [
                r["material_phase"] for r in override_observed if r["fired"]
            ],
            "pack_on_disk_is_untouched": (
                "the shim deep-copies the parsed pack and rewrites the copy; project-pack/ is owner "
                "data and is never written"
            ),
        },
    }
    negative_control = {
        "probe": (
            "run the same interval predicate over a scheduler that fires on every material phase "
            "and over one that never fires"
        ),
        "why": (
            "'a review fired after three material phases' is satisfied by a scheduler that fires "
            "after one, and after two, and after every phase thereafter. The property is the "
            "silence as much as the fire, so the control must be caught at material phases "
            f"{early_phases} specifically."
        ),
        "fires_every_phase": {
            "material_phases_observed": eager_observed,
            "detector_findings": eager_findings,
            "caught_at_early_phases": early_caught,
        },
        "never_fires": {
            "material_phases_observed": deaf_observed,
            "detector_findings": deaf_findings,
            "caught_the_missed_review": deaf_caught,
        },
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": _binding_evidence(
            ctx, execution_log, negative_control
        ),
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"a periodic review fires at material phases {fired_at} and at none of {silent_at}, on "
            f"the interval of {interval} the pack declares; non-material phases do not advance the "
            f"counter, a pack declaring {_OVERRIDE_INTERVAL} moves the first fire to phase "
            f"{_OVERRIDE_INTERVAL}, and a scheduler that fires every phase is caught at phases "
            f"{early_phases}"
        ),
    )


# ===========================================================================
# A2 — a review fires at an event trigger such as after_walking_skeleton
# ===========================================================================


def _event_probe(
    scheduler: ContractReviewScheduler, declared: list[str], undeclared: tuple[str, ...]
) -> tuple[dict[str, Any], list[str]]:
    """The predicate behind A2: every declared event fires, nothing else does.

    Shared by the positive arm and both controls. The undeclared probes are the
    load-bearing half -- ``due_for_event`` returning a trigger for any string
    would satisfy "a review fires at after_walking_skeleton" while meaning the
    scheduler is not event-triggered at all.
    """
    findings: list[str] = []
    declared_records: dict[str, Any] = {}
    for event in declared:
        trigger = scheduler.due_for_event(event)
        declared_records[event] = _trigger_record(trigger)
        if trigger is None:
            findings.append(f"the pack declares event trigger {event!r} and no review fired for it")
            continue
        if trigger.trigger_type != "event":
            findings.append(
                f"{event!r} produced a trigger typed {trigger.trigger_type!r}, not 'event'"
            )
        if trigger.event != event:
            findings.append(
                f"{event!r} produced a trigger carrying event {trigger.event!r}; the review would "
                "be attributed to a trigger that did not occur"
            )
        if not trigger.trigger_id.startswith("CRT-EV-"):
            findings.append(
                f"{event!r} produced trigger id {trigger.trigger_id!r}, which is not an event "
                "trigger identifier"
            )

    undeclared_records: dict[str, Any] = {}
    for event in undeclared:
        trigger = scheduler.due_for_event(event)
        undeclared_records[event] = _trigger_record(trigger)
        if trigger is not None:
            findings.append(
                f"the undeclared event {event!r} fired a review ({trigger.trigger_id}); a scheduler "
                "that fires on any string is not firing on the contract's triggers"
            )
    return {"declared": declared_records, "undeclared": undeclared_records}, findings


def d2_22_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``event_trigger_probe`` -> ``review_fired_on_event``.

    Every pack-declared trigger is probed, not only the one the claim names, and
    six undeclared near misses must all return nothing. The named event is then
    carried through an actual review, because a ``ReviewTrigger`` no review is
    ever built from is a scheduling opinion rather than a review that fired.
    """
    pack = _pack(ctx.repo_root)
    contract_review = pack.yaml("contract.yaml").get("contract_review") or {}
    declared = [str(e) for e in contract_review.get("event_triggers", [])]
    scheduler = ContractReviewScheduler.from_pack(pack)

    findings: list[str] = []
    if not declared:
        findings.append(
            "contract.yaml declares no contract_review.event_triggers, so 'a review fires at an "
            "event trigger' has nothing to fire on"
        )
    if tuple(scheduler.event_triggers) != tuple(declared):
        findings.append(
            f"the scheduler carries triggers {list(scheduler.event_triggers)} while the pack "
            f"declares {declared}"
        )
    if _NAMED_EVENT not in a.claim:
        findings.append(
            f"the assertion no longer names {_NAMED_EVENT!r} ({a.claim!r}); this check would be "
            "probing an event the gate does not name"
        )
    if _NAMED_EVENT not in declared:
        findings.append(f"the pack does not declare the event trigger {_NAMED_EVENT!r}")

    records, probe_findings = _event_probe(scheduler, declared, _UNDECLARED_EVENTS)
    findings.extend(probe_findings)

    # The named event, carried through a real review.
    requirements = list(_requirement_ids(ctx.repo_root))
    named_trigger = scheduler.due_for_event(_NAMED_EVENT)
    review_record: dict[str, Any] = {"review_ran": False}
    if named_trigger is None:
        findings.append(f"no review fired at {_NAMED_EVENT!r}, the event the assertion names")
    else:
        review = scheduler.review(
            review_id="CR-D2-22-A2",
            trigger=named_trigger,
            drift_findings=[],
            requirements_before=requirements,
            requirements_after=requirements,
            evidence=["GATE-D2-22 A2 event_trigger_probe"],
        )
        review_record = {"review_ran": True, **_review_record(review)}
        if review.trigger is not named_trigger:
            findings.append("the review was not built from the trigger the event produced")
        if review.trigger.event != _NAMED_EVENT or review.trigger.trigger_type != "event":
            findings.append(
                f"the review records trigger {review.trigger.as_body()}, which is not the "
                f"{_NAMED_EVENT!r} event trigger"
            )
        if review.outcome is not ADVANCING_OUTCOME:
            findings.append(
                f"a conformance review at {_NAMED_EVENT!r} with nothing wrong produced "
                f"{review.outcome}; the event arm is failing for a reason A2 does not name"
            )

    # Negative controls: the same predicate over two broken schedulers.
    anything_records, anything_findings = _event_probe(
        _SchedulerThatFiresOnAnyEvent.from_pack(pack), declared, _UNDECLARED_EVENTS
    )
    caught_undeclared = {
        event: any(f"{event!r} fired a review" in f for f in anything_findings)
        for event in _UNDECLARED_EVENTS
    }
    deaf_records, deaf_findings = _event_probe(
        _SchedulerThatNeverFiresOnEvents.from_pack(pack), declared, _UNDECLARED_EVENTS
    )
    caught_named = any(
        f"declares event trigger {_NAMED_EVENT!r} and no review fired" in f for f in deaf_findings
    )

    missed = sorted(event for event, caught in caught_undeclared.items() if not caught)
    if missed:
        findings.append(
            f"negative control did not fire for {missed}: a scheduler returning a trigger for any "
            "string was not distinguished from one honouring the pack's triggers"
        )
    if not caught_named:
        findings.append(
            "negative control did not fire: a scheduler that honours no event at all was not "
            f"reported as failing to fire at {_NAMED_EVENT!r}"
        )

    execution_log = {
        "check": a.method or "event_trigger_probe",
        "expected": a.expected,
        "contract_ref": "contract.md#19.3",
        "pack_declared_event_triggers": declared,
        "pack_declared_event_trigger_count": len(declared),
        "scheduler_event_triggers": list(scheduler.event_triggers),
        "assertion_names_event": _NAMED_EVENT,
        "triggers_probed": records,
        "declared_events_that_fired": [
            event for event, record in records["declared"].items() if record["fired"]
        ],
        "undeclared_events_that_fired": [
            event for event, record in records["undeclared"].items() if record["fired"]
        ],
        "review_built_from_the_named_event": review_record,
    }
    negative_control = {
        "probe": (
            "run the same event predicate over a scheduler returning a trigger for every string "
            "and over one returning none, plus six undeclared near-miss events against the real "
            "scheduler"
        ),
        "why": (
            "'a review fires at after_walking_skeleton' is satisfied by a scheduler that fires at "
            "every string ever passed to it. The undeclared probes are prefix, suffix, case and "
            "whitespace variants of real triggers, so a loose match is caught as well as a "
            "fire-on-anything."
        ),
        "undeclared_probes": list(_UNDECLARED_EVENTS),
        "real_scheduler_refused_all_undeclared_events": not [
            event for event, record in records["undeclared"].items() if record["fired"]
        ],
        "fires_on_any_event": {
            "triggers_probed": anything_records["undeclared"],
            "detector_findings": anything_findings,
            "caught_undeclared": caught_undeclared,
        },
        "never_fires_on_events": {
            "triggers_probed": deaf_records["declared"],
            "detector_findings": deaf_findings,
            "caught_the_named_event": caught_named,
        },
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": _binding_evidence(
            ctx, execution_log, negative_control
        ),
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"all {len(declared)} pack-declared event triggers fire a typed event trigger, "
            f"{len(_UNDECLARED_EVENTS)} undeclared near-miss events fire nothing, and the "
            f"{_NAMED_EVENT!r} trigger carries through to a real ContractReview"
        ),
    )


# ===========================================================================
# A3 — an outcome other than CONTRACT_REAFFIRMED halts automatic advance
# ===========================================================================


def _outcome_reviews(
    scheduler: ContractReviewScheduler,
    trigger: ReviewTrigger,
    requirements: list[str],
    drift_finding: dict[str, Any],
    review_class: type[ContractReview] | None = None,
) -> dict[ContractReviewOutcome, ContractReview]:
    """One review per Section 19.4 outcome, each derived by the real deriver.

    The outcomes are not constructed by hand: each arm passes an observed
    condition -- an injected drift finding, stale evidence, changed risk, an
    ambiguity, an amendment -- and the scheduler decides. ``review_class`` lets
    the negative control re-wrap the same derived data in a broken review type
    without touching the derivation.
    """
    conditions: dict[ContractReviewOutcome, dict[str, Any]] = {
        ContractReviewOutcome.CONTRACT_REAFFIRMED: {},
        ContractReviewOutcome.DRIFT_DETECTED: {"drift_findings": [drift_finding]},
        ContractReviewOutcome.EVIDENCE_STALE: {"stale_evidence": True},
        ContractReviewOutcome.RISK_CHANGED: {"risk_changed": True},
        ContractReviewOutcome.CONTRACT_AMBIGUITY: {"ambiguity": True},
        ContractReviewOutcome.AMENDMENT_REQUIRED: {"amendment_required": True},
    }
    reviews: dict[ContractReviewOutcome, ContractReview] = {}
    for intended, condition in conditions.items():
        flags = {k: v for k, v in condition.items() if k != "drift_findings"}
        review = scheduler.review(
            review_id=f"CR-D2-22-{intended}",
            trigger=trigger,
            drift_findings=condition.get("drift_findings", []),
            requirements_before=list(requirements),
            requirements_after=list(requirements),
            evidence=["GATE-D2-22 A3 negative_control_inject_drift"],
            **flags,
        )
        if review_class is not None:
            review = review_class(
                review_id=review.review_id,
                trigger=review.trigger,
                outcome=review.outcome,
                findings=list(review.findings),
                requirements_before=list(review.requirements_before),
                requirements_after=list(review.requirements_after),
                evidence=list(review.evidence),
            )
        reviews[intended] = review
    return reviews


def _halt_findings(reviews: dict[ContractReviewOutcome, ContractReview], label: str) -> list[str]:
    """The predicate behind A3, run over whatever set of reviews it is handed.

    Exactly one outcome may advance automatically. Every other outcome must halt
    *and* name the typed remediation route Section 19.4 assigns it -- a halt with
    no route is a stall, not a route to remediation.
    """
    findings: list[str] = []
    for intended, review in reviews.items():
        body = review.as_body()
        if review.outcome is not intended:
            findings.append(
                f"{label}: the observed condition for {intended} derived outcome {review.outcome}"
            )
            continue
        advances = review.advances_automatically
        by_class = ContractReviewScheduler.advances_automatically(review.outcome)
        if advances is not (intended is ADVANCING_OUTCOME):
            findings.append(
                f"{label}: {intended} reports advances_automatically={advances}; only "
                f"{ADVANCING_OUTCOME} may advance automatically"
            )
        if by_class is not advances:
            findings.append(
                f"{label}: the review says advances_automatically={advances} for {intended} while "
                f"the scheduler says {by_class}"
            )
        if intended is ADVANCING_OUTCOME:
            if body["remediation_route"] is not None:
                findings.append(
                    f"{label}: {intended} advances automatically and still names remediation route "
                    f"{body['remediation_route']!r}"
                )
            continue
        route = body["remediation_route"]
        expected_route = REMEDIATION_ROUTE.get(intended)
        if not route:
            findings.append(
                f"{label}: {intended} halts automatic advance and names no remediation route"
            )
        elif route != expected_route:
            findings.append(
                f"{label}: {intended} routes to {route!r}, not to the Section 19.4 route "
                f"{expected_route!r}"
            )
        if intended in TERMINAL_ROUTE and body["terminal_state"] != str(TERMINAL_ROUTE[intended]):
            findings.append(
                f"{label}: {intended} is terminal and records terminal state "
                f"{body['terminal_state']!r}, not {str(TERMINAL_ROUTE[intended])!r}"
            )
    return findings


def _graph_probe(repo_root: Path) -> dict[str, Any]:
    """Run the real ``contract_revalidation_graph`` and measure how a halt is expressed.

    Two runs through one compiled graph: one where the frozen contract hash no
    longer matches the live pack, one where it does. The first must classify
    DRIFT_DETECTED and write a typed blocker; the second must not, or "a typed
    blocker is raised" would describe a graph that always raises one.

    The project graph's edges are read as well, because A3's ``expected`` says
    ``advance_halted`` and the honest measurement is that the
    ``contract_revalidation -> planning`` edge is unconditional. What halts is
    state, not traversal, and the evidence says so.
    """
    from workflows.graphs._common import WorkflowServices
    from workflows.graphs.contract import build_contract_revalidation_graph
    from workflows.graphs.project import build_project_graph
    from workflows.state import initial_state

    services = WorkflowServices(pack_root=repo_root / "project-pack")
    pack = services.pack
    live = {
        "contract_id": pack.contract_id,
        "contract_version": pack.contract_version,
        "contract_md_hash": pack.files["contract.md"].content_hash,
        "contract_yaml_hash": pack.files["contract.yaml"].content_hash,
    }
    compiled = build_contract_revalidation_graph(services).compile()

    def run(frozen: dict[str, Any]) -> dict[str, Any]:
        state = initial_state(
            project_id=pack.project_id,
            project_version=str(pack.yaml("project.yaml")["project"].get("version", "1")),
            contract_version=pack.contract_version,
            terminus_database="efah",
            terminus_branch="import/pack",
            terminus_commit="unresolved-on-build-side",
            work_unit_id="WU-GATE-D2-22",
            graph_id="contract_revalidation_graph",
        )
        state["artifacts"] = {"frozen_contract": frozen}
        result = compiled.invoke(dict(state))
        return {
            "contract_review_outcome": result.get("artifacts", {}).get("contract_review_outcome"),
            "typed_blockers": list(result.get("typed_blockers", [])),
            "owner_interrupts": list(result.get("owner_interrupts", [])),
            "node_log": list(result.get("node_log", [])),
        }

    drifted = run({**live, "contract_md_hash": "sha256:" + "0" * 64})
    reaffirmed = run(dict(live))

    builder = build_project_graph(services)
    outgoing = sorted(
        target for source, target in builder.edges if source == "contract_revalidation"
    )
    branch_sources = sorted(getattr(builder, "branches", {}))
    return {
        "drifted_run": drifted,
        "reaffirmed_run": reaffirmed,
        "outgoing_edges_from_contract_revalidation": outgoing,
        "conditional_branch_sources": branch_sources,
        "advance_edge_is_conditional": "contract_revalidation" in branch_sources,
    }


def d2_22_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``negative_control_inject_drift`` -> ``advance_halted_and_typed_remediation_raised``.

    Drift is injected into a real review and the whole Section 19.4 outcome set
    is swept, because "an outcome other than CONTRACT_REAFFIRMED halts" is a
    claim about *every* other outcome, not about the one that is convenient to
    produce. Each non-advancing outcome must also name its typed remediation
    route: a halt with no route is a stall.

    Honest limit, recorded in the evidence rather than left for a reader to
    infer: the project graph's ``contract_revalidation -> planning`` edge is
    unconditional. Nothing here stops graph traversal, and this check does not
    claim it does. The halt is state -- ``advances_automatically`` False, a named
    remediation route, and a typed blocker written by the revalidation graph and
    surfaced by the control-plane projection.
    """
    pack = _pack(ctx.repo_root)
    scheduler = ContractReviewScheduler.from_pack(pack)
    requirements = list(_requirement_ids(ctx.repo_root))
    trigger = None
    for _ in range(scheduler.interval_material_phases):
        trigger = scheduler.observe_phase()
    drift_finding = {
        "finding": str(DriftFinding.UNLINKED_TASK),
        "subject": "TSK-GATE-D2-22-PROBE",
        "detail": "injected for the GATE-D2-22 A3 negative control",
        "contract_ref": "contract.md#19.2",
    }

    findings: list[str] = []
    if trigger is None:
        findings.append(
            f"no periodic trigger was produced after {scheduler.interval_material_phases} material "
            "phases, so A3 has no real review to run"
        )
        trigger = ReviewTrigger(
            trigger_id="CRT-INTERVAL",
            trigger_type="periodic",
            reason="fallback: the scheduler produced no trigger",
            phases_since_last_review=scheduler.interval_material_phases,
        )

    reviews = _outcome_reviews(scheduler, trigger, requirements, drift_finding)
    findings.extend(_halt_findings(reviews, "Section 19.4 outcome sweep"))

    # Route coverage: every non-advancing outcome in the enum must have a route,
    # and the advancing one must not have acquired one.
    non_advancing = {o for o in ContractReviewOutcome if o is not ADVANCING_OUTCOME}
    missing_routes = sorted(str(o) for o in non_advancing - set(REMEDIATION_ROUTE))
    if missing_routes:
        findings.append(f"Section 19.4 outcomes with no typed remediation route: {missing_routes}")
    if ADVANCING_OUTCOME in REMEDIATION_ROUTE:
        findings.append(
            f"{ADVANCING_OUTCOME} carries a remediation route; the outcome that advances "
            "automatically has nothing to remediate"
        )
    unswept = sorted(str(o) for o in set(ContractReviewOutcome) - set(reviews))
    if unswept:
        findings.append(f"Section 19.4 outcomes never exercised by this check: {unswept}")

    injected = reviews[ContractReviewOutcome.DRIFT_DETECTED]
    if injected.findings != [drift_finding]:
        findings.append(f"the injected drift finding did not reach the review: {injected.findings}")

    graph = _graph_probe(ctx.repo_root)
    drifted = graph["drifted_run"]
    reaffirmed = graph["reaffirmed_run"]
    if drifted["contract_review_outcome"] != str(ContractReviewOutcome.DRIFT_DETECTED):
        findings.append(
            "the contract_revalidation graph classified a contract whose hash no longer matches "
            f"the frozen one as {drifted['contract_review_outcome']!r}"
        )
    if str(DriftFinding.STALE_CONTRACT_VERSION) not in drifted["typed_blockers"]:
        findings.append(
            f"a drifted revalidation run wrote typed blockers {drifted['typed_blockers']}, without "
            f"{DriftFinding.STALE_CONTRACT_VERSION!s}; nothing typed halts the advance"
        )
    if reaffirmed["contract_review_outcome"] != str(ADVANCING_OUTCOME):
        findings.append(
            "negative control failed: a revalidation run against the live pack classified "
            f"{reaffirmed['contract_review_outcome']!r} rather than {ADVANCING_OUTCOME!s}"
        )
    if reaffirmed["typed_blockers"]:
        findings.append(
            "negative control failed: a reaffirming revalidation run still wrote typed blockers "
            f"{reaffirmed['typed_blockers']}; a graph that always blocks proves nothing about drift"
        )

    # Negative control on the predicate itself: the same derived outcomes,
    # re-wrapped in a review type that advances whatever it found.
    broken = _outcome_reviews(
        scheduler, trigger, requirements, drift_finding, review_class=_ReviewThatAlwaysAdvances
    )
    broken_findings = _halt_findings(broken, "negative control: advances on every outcome")
    caught_drift = any(
        str(ContractReviewOutcome.DRIFT_DETECTED) in f and "may advance automatically" in f
        for f in broken_findings
    )
    if not caught_drift:
        findings.append(
            "negative control did not fire: a review that advances on every outcome was not "
            f"reported as advancing on {ContractReviewOutcome.DRIFT_DETECTED!s}"
        )

    clean = reviews[ContractReviewOutcome.CONTRACT_REAFFIRMED]
    if not clean.advances_automatically:
        findings.append(
            "negative control failed: a clean review did not advance automatically, so 'an outcome "
            "other than CONTRACT_REAFFIRMED halts' would only mean nothing ever advances"
        )

    execution_log = {
        "check": a.method or "negative_control_inject_drift",
        "expected": a.expected,
        "contract_ref": "contract.md#19.4",
        "trigger": trigger.as_body(),
        "injected_drift_finding": drift_finding,
        "outcome_sweep": {str(o): _review_record(r) for o, r in reviews.items()},
        "advancing_outcome": str(ADVANCING_OUTCOME),
        "outcomes_that_advanced": [str(o) for o, r in reviews.items() if r.advances_automatically],
        "typed_remediation_routes": {str(o): route for o, route in REMEDIATION_ROUTE.items()},
        "terminal_routes": {str(o): str(state) for o, state in TERMINAL_ROUTE.items()},
        "runtime_graph": {
            "drifted_run": drifted,
            "typed_blocker_raised": drifted["typed_blockers"],
        },
        "how_the_halt_is_expressed": {
            "measured": (
                "the project graph's only outgoing edge from contract_revalidation is "
                f"{graph['outgoing_edges_from_contract_revalidation']} and it is unconditional: "
                f"conditional branches are registered on {graph['conditional_branch_sources']} only"
            ),
            "outgoing_edges_from_contract_revalidation": graph[
                "outgoing_edges_from_contract_revalidation"
            ],
            "conditional_branch_sources": graph["conditional_branch_sources"],
            "advance_edge_is_conditional": graph["advance_edge_is_conditional"],
            "limit": (
                "a non-advancing outcome halts by state, not by traversal: "
                "ContractReview.advances_automatically is False, a typed remediation route is "
                "named, and the revalidation graph writes a typed blocker that the control-plane "
                "projection surfaces to the owner. This check does NOT claim the graph stops at "
                "contract_revalidation, because it does not -- the edge to planning runs "
                "unconditionally. Making the halt structural would need a conditional edge there."
            ),
        },
    }
    negative_control = {
        "probe": (
            "inject a real Section 19.2 drift finding into a review, sweep every Section 19.4 "
            "outcome, drift the frozen contract hash through the real revalidation graph, and "
            "re-wrap the same derived outcomes in a review type that always advances"
        ),
        "why": (
            "'a non-reaffirming outcome halts' is satisfied by a system that never advances at "
            "all, and 'a typed blocker is raised' by a graph that always raises one. The clean "
            "review must advance, the reaffirming graph run must raise nothing, and the "
            "always-advancing review type must be caught."
        ),
        "injected_drift_review": _review_record(injected),
        "clean_review_still_advances": _review_record(clean),
        "reaffirming_graph_run": reaffirmed,
        "always_advancing_review": {
            "detector_findings": broken_findings,
            "caught_drift_detected": caught_drift,
        },
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": _binding_evidence(
            ctx, execution_log, negative_control
        ),
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"injected drift yields {ContractReviewOutcome.DRIFT_DETECTED!s} with "
            "advances_automatically False and remediation route "
            f"{REMEDIATION_ROUTE[ContractReviewOutcome.DRIFT_DETECTED]!r}; all "
            f"{len(non_advancing)} non-advancing outcomes halt and name a typed route while "
            f"{ADVANCING_OUTCOME!s} still advances, and the real revalidation graph writes "
            f"{DriftFinding.STALE_CONTRACT_VERSION!s} on drift and nothing on reaffirmation. "
            "Honest limit: the halt is state, not a stopped edge -- contract_revalidation -> "
            "planning is unconditional"
        ),
    )


# ===========================================================================
# A4 — review does not add optional improvements to scope
# ===========================================================================


def _scope_findings(
    review: ContractReview, before: list[str], after: list[str], label: str
) -> list[str]:
    """The predicate behind A4, recomputed from the review's own inputs.

    ``ContractReview.added_requirements`` is the object's claim about itself.
    This recomputes the diff from the requirement lists that went in and treats a
    disagreement as a finding, so a review that simply reported ``[]`` could not
    satisfy the assertion. Where requirements *were* added, Section 19.4 makes
    that drift: the outcome must not advance and the review must carry the
    ``UNAPPROVED_SCOPE_EXPANSION`` finding by name.
    """
    findings: list[str] = []
    recomputed = sorted(set(after) - set(before))
    body = review.as_body()
    if review.added_requirements != recomputed:
        findings.append(
            f"{label}: the review reports added_requirements {review.added_requirements} while a "
            f"recomputation over its own requirement lists finds {recomputed}"
        )
    if review.scope_expanded is not bool(recomputed):
        findings.append(
            f"{label}: scope_expanded={review.scope_expanded} with {len(recomputed)} requirement(s) "
            "added"
        )
    if recomputed:
        if review.outcome is ADVANCING_OUTCOME:
            findings.append(
                f"{label}: a review that added {recomputed} reaffirmed the contract and advanced "
                "automatically; Section 19.4 makes an added requirement drift"
            )
        if body["scope_expansion_finding"] != str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION):
            findings.append(
                f"{label}: a review that added {recomputed} records scope_expansion_finding "
                f"{body['scope_expansion_finding']!r}, not "
                f"{str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION)!r}"
            )
        if not body["remediation_route"]:
            findings.append(f"{label}: a scope-expanding review names no remediation route")
    else:
        if body["scope_expansion_finding"] is not None:
            findings.append(
                f"{label}: no requirement was added and the review records scope_expansion_finding "
                f"{body['scope_expansion_finding']!r}"
            )
        if body["added_requirements"]:
            findings.append(
                f"{label}: no requirement was added and the review body lists "
                f"{body['added_requirements']}"
            )
    return findings


def d2_22_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``review_output_scope_diff`` -> ``zero_new_requirements_from_review``.

    The subject is the project's own compiled requirement set, not a pair of
    invented ids. A conformance review over it must add nothing; the same review
    with one optional improvement appended must be forced to DRIFT_DETECTED and
    carry ``UNAPPROVED_SCOPE_EXPANSION``. Both arms run one predicate that
    recomputes the diff from the requirement lists rather than trusting the
    review's report of itself.

    Honest limit, recorded in the evidence: the diff is one-directional. A review
    that *drops* a requirement adds nothing and is not caught here -- that is
    ``REQUIREMENT_WEAKENING``, a different Section 19.2 finding, and this gate
    does not measure it.
    """
    pack = _pack(ctx.repo_root)
    scheduler = ContractReviewScheduler.from_pack(pack)
    requirements = list(_requirement_ids(ctx.repo_root))
    trigger = ReviewTrigger(
        trigger_id="CRT-INTERVAL",
        trigger_type="periodic",
        reason=f"{scheduler.interval_material_phases} material phases completed",
        phases_since_last_review=scheduler.interval_material_phases,
    )

    findings: list[str] = []
    if not requirements:
        findings.append(
            "the pack compiles to no Requirement nodes, so 'zero new requirements from review' is "
            "vacuous"
        )

    def review_over(after: list[str], review_id: str) -> ContractReview:
        return scheduler.review(
            review_id=review_id,
            trigger=trigger,
            drift_findings=[],
            requirements_before=requirements,
            requirements_after=after,
            evidence=["GATE-D2-22 A4 review_output_scope_diff"],
        )

    conformance = review_over(list(requirements), "CR-D2-22-A4-CONFORMANCE")
    findings.extend(
        _scope_findings(conformance, requirements, list(requirements), "conformance review")
    )
    if conformance.outcome is not ADVANCING_OUTCOME:
        findings.append(
            f"a conformance review that changed nothing produced {conformance.outcome}; A4 is "
            "failing for a reason it does not name"
        )

    expanded_after = [*requirements, _OPTIONAL_IMPROVEMENT]
    expanded = review_over(expanded_after, "CR-D2-22-A4-IMPROVEMENT")
    findings.extend(_scope_findings(expanded, requirements, expanded_after, "improvement review"))
    if expanded.outcome is not ContractReviewOutcome.DRIFT_DETECTED:
        findings.append(
            f"a review adding {_OPTIONAL_IMPROVEMENT!r} with no drift finding of its own produced "
            f"{expanded.outcome}, not {ContractReviewOutcome.DRIFT_DETECTED!s}"
        )
    if expanded.advances_automatically:
        findings.append("a review that added a requirement advanced automatically")

    # A removal is not an addition. Measured rather than asserted in prose, so
    # the one-directional limit is recorded from an observation.
    removal_after = list(requirements[1:])
    removal = review_over(removal_after, "CR-D2-22-A4-REMOVAL") if requirements else conformance
    if requirements:
        findings.extend(_scope_findings(removal, requirements, removal_after, "removal review"))

    # Negative control on the predicate: the same expanded requirement lists in a
    # review type that reports no additions at all.
    silent = _ReviewIgnoringAddedRequirements(
        review_id="CR-D2-22-A4-SILENT",
        trigger=trigger,
        outcome=ADVANCING_OUTCOME,
        findings=[],
        requirements_before=list(requirements),
        requirements_after=list(expanded_after),
        evidence=["negative control"],
    )
    silent_findings = _scope_findings(silent, requirements, expanded_after, "negative control")
    caught_silent = any("recomputation over its own requirement lists" in f for f in silent_findings)
    if not caught_silent:
        findings.append(
            "negative control did not fire: a review reporting no added requirements while its own "
            "lists differ was not caught, so A4 would be reading the review's claim about itself"
        )

    execution_log = {
        "check": a.method or "review_output_scope_diff",
        "expected": a.expected,
        "contract_ref": "contract.md#19.4",
        "requirement_source": "contracts.compiler.compile_pack over the owner's project pack",
        "requirements_before_count": len(requirements),
        "conformance_review": {
            **_review_record(conformance),
            "requirements_after_count": len(conformance.requirements_after),
            "recomputed_added": sorted(set(conformance.requirements_after) - set(requirements)),
        },
        "scope_diff_is_one_directional": {
            "removal_review": _review_record(removal) if requirements else None,
            "limit": (
                "added_requirements is set(after) - set(before), so a review that drops a "
                "requirement reports zero additions and reaffirms. That is REQUIREMENT_WEAKENING "
                "under Section 19.2, a different finding; this gate asks only that a review adds "
                "nothing, and this check does not claim to detect removals."
            ),
        },
    }
    negative_control = {
        "probe": (
            f"the same review with one optional improvement, {_OPTIONAL_IMPROVEMENT!r}, appended to "
            "the requirement set, and the same expanded lists in a review type that reports no "
            "additions"
        ),
        "why": (
            "'zero new requirements' is satisfied for free by a review that never diffs anything, "
            "and by an object that reports [] regardless. The first arm must be forced to "
            f"{ContractReviewOutcome.DRIFT_DETECTED!s} with "
            f"{DriftFinding.UNAPPROVED_SCOPE_EXPANSION!s}, and the second must be caught by "
            "recomputation."
        ),
        "improvement_review": {
            **_review_record(expanded),
            "requirements_after_count": len(expanded_after),
            "recomputed_added": sorted(set(expanded_after) - set(requirements)),
        },
        "review_reporting_no_additions": {
            "detector_findings": silent_findings,
            "detector_fires": caught_silent,
        },
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": _binding_evidence(
            ctx, execution_log, negative_control
        ),
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"a conformance review over the project's {len(requirements)} compiled requirements "
            "adds none and reaffirms, while appending one optional improvement forces "
            f"{ContractReviewOutcome.DRIFT_DETECTED!s} with "
            f"{DriftFinding.UNAPPROVED_SCOPE_EXPANSION!s} and no automatic advance"
        ),
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D2_22: dict[tuple[str, str], Check] = {
    ("GATE-D2-22", "A1"): d2_22_a1,
    ("GATE-D2-22", "A2"): d2_22_a2,
    ("GATE-D2-22", "A3"): d2_22_a3,
    ("GATE-D2-22", "A4"): d2_22_a4,
}
