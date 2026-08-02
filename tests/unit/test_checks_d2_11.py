"""GATE-D2-11's checks, and the proof that they can fail.

Contract Sections 4.1, 9.8, 11.6 · Section 18. A gate check that passes against
the real projection stack tells you very little on its own: the same green would
appear if the check compared a constant to itself. So every check here is
exercised twice -- once against the real seam and once against a subject where
the property the assertion names is false -- and the second run is what gives
the first its meaning.

Each broken subject is broken in exactly one way, and the way is named:

* a control plane that reports nothing, so the thirteen views are present and
  empty (A1) -- plus a run where the *negative control* itself is defeated, to
  show that arm is load-bearing rather than decorative;
* a read-only source that forwards every attribute to the control plane behind
  it (A2), a projection that writes back through that same handle, and a
  projection pass that publishes nothing at all;
* a redaction guard that passes everything (A3), and one that rejects everything
  -- because both are broken, and only one of them looks broken;
* a duration table that fills the underivable values with a number, and a timing
  record that accepts an agent estimate (A4).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ConfigDict

from api.adapters.control_plane_memory import InMemoryControlPlane
from api.state import ControlPlaneSnapshot, TimingRecord
from dashboard.redaction import ProtectedContentLeak
from dashboard.source import ReadOnlySource
from dashboard.views import REQUIRED_VIEWS
from evaluation import checks_d2_11
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d2_11 import CHECKS_D2_11, REQUIRED_CONTENT
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict
from integrations.plane import PlaneProjection, ProjectionResult

GATE_ID = "GATE-D2-11"


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context. The candidate commit is a stand-in because these tests are
    about the checks; the gate-runner test at the end binds to the real HEAD."""
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D2_11[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


# --- broken subjects -------------------------------------------------------


class ControlPlaneWithNothingInIt:
    """A control plane that answers every read with an empty project.

    The shape of a dashboard wired to a store nobody has written to: the thirteen
    views build, they type-check, and there is nothing in any of them.
    """

    def snapshot(self, project_id: str) -> ControlPlaneSnapshot:
        return checks_d2_11._empty_snapshot()

    def graph(self, project_id: str) -> None:
        return None

    def impact_map(self, dependency_id: str) -> None:
        return None


class LeakyReadOnlySource(ReadOnlySource):
    """A wrapper that forwards whatever it is asked for.

    Exactly the mistake ``ReadOnlySource`` exists to prevent: the write half is
    one ``__getattr__`` away, and every method on it mutates authoritative state.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ProjectionThatWritesBack(PlaneProjection):
    """Reaches through the read-only handle and edits authoritative state.

    The private attribute is the realistic route: nothing stops a future edit
    from unwrapping the source it was handed.
    """

    def project(self, snapshot: ControlPlaneSnapshot) -> ProjectionResult:
        from api.state import TaskRecord
        from governance.states import TaskState

        self._source._inner.upsert_task(  # type: ignore[union-attr]
            TaskRecord(
                task_id="TSK-WRITTEN-BY-THE-PROJECTION",
                project_id=snapshot.project.project_id,
                title="written back from Plane",
                state=TaskState.PROPOSED,
            )
        )
        return super().project(snapshot)


class ProjectionThatPublishesNothing(PlaneProjection):
    """Reports success without touching Plane.

    'Authoritative state unchanged' is free for a pass that never ran, which is
    why A2 counts what the pass published.
    """

    def project(self, snapshot: ControlPlaneSnapshot) -> ProjectionResult:
        return ProjectionResult(status="projected", project_id=snapshot.project.project_id)


def guard_that_passes_everything(payload: Any, *, where: str) -> None:
    """A redaction guard somebody disabled."""


def guard_that_rejects_everything(payload: Any, *, where: str) -> None:
    """A guard that publishes nothing. Just as broken, and it looks strict."""
    raise ProtectedContentLeak(where, "everything")


def guessing_durations(timing: TimingRecord) -> dict[str, float | None]:
    """Fills in a number wherever the system events give nothing."""
    honest = checks_d2_11._recompute_durations(timing)
    return {key: (value if value is not None else 900.0) for key, value in honest.items()}


class TimingRecordWithAnEstimate(TimingRecord):
    """The field Section 9.8 forbids, added the way an adapter would add it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimated_hours: float | None = None


# --- the registry ----------------------------------------------------------


def test_the_registry_covers_every_assertion_the_pack_declares(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D2_11 if gid == GATE_ID}
    assert registered == declared
    assert all(gid == GATE_ID for gid, _ in CHECKS_D2_11)


def test_this_module_does_not_shadow_an_existing_registration():
    """Merging this map must add checks, never silently replace one.

    ``dict.update`` wins without a word, so a key that is already in ``CHECKS``
    and resolves to something else is the hazard worth catching here.
    """
    for key, check in CHECKS_D2_11.items():
        assert CHECKS.get(key, check) is check, f"{key} already resolves to a different check"


def test_the_module_imports_from_either_side_of_the_cycle():
    """The circular-import trap, measured rather than hoped for.

    ``checks.py`` imports this module to register it, so a module-scope import of
    ``evaluation.checks`` here would work from one direction and explode from the
    other, depending on which side Python happened to load first. Each direction
    runs in its own interpreter, because proving it inside this one would mean
    tearing modules out of ``sys.modules`` and every enum identity in the session
    with them.
    """
    import subprocess
    import sys

    for first, second in (
        ("evaluation.checks_d2_11", "evaluation.checks"),
        ("evaluation.checks", "evaluation.checks_d2_11"),
    ):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {first}; import {second}; print('ok')"],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, f"{first} then {second}: {proc.stderr[-600:]}"


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_projection(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["views_populated"] == len(REQUIRED_VIEWS) == 13
    assert log["views_unpopulated"] == []
    assert log["views_are_required_fields"]
    assert log["a_projection_missing_one_view_is_refused"]["refused"]
    assert log["required_views_declared_in_plane_yaml"] == list(REQUIRED_VIEWS)
    assert set(REQUIRED_CONTENT) == set(REQUIRED_VIEWS)


def test_a1_fails_when_the_views_are_present_but_empty(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d2_11,
        "_seeded_control_plane",
        lambda repo_root: (ControlPlaneWithNothingInIt(), "EMPTY"),
    )
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("present but carries nothing" in f for f in outcome.findings)
    # Every one of the thirteen, not a lucky one.
    assert sum("present but carries nothing" in f for f in outcome.findings) == 13


def test_a1_negative_control_is_load_bearing(ctx, gate, monkeypatch):
    """Defeat the empty-projection control and the check must refuse to pass.

    Without this, A1 would still be green on a predicate that counted fields
    rather than content -- the exact failure the control exists to rule out.
    """
    control_plane, project_id = checks_d2_11._seeded_control_plane(ctx.repo_root)
    monkeypatch.setattr(
        checks_d2_11, "_empty_snapshot", lambda: control_plane.snapshot(project_id)
    )
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in f for f in outcome.findings)


def test_a1_records_what_populated_does_and_does_not_claim(ctx, gate):
    """The caveat is evidence, not a comment somebody can delete."""
    log = run(ctx, gate, "A1").evidence["gate_execution_log"]
    assert "empty drift view" in log["what_populated_does_not_mean"]
    assert "seeded" in log["what_the_subject_is"]


# --- A2 --------------------------------------------------------------------


def test_a2_passes_against_the_real_seams(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["unchanged"]
    assert log["read_surface_size"] == 8
    assert all(not record["reachable"] for record in log["write_attempts"].values())
    assert log["seams"]["plane_projection_refuses_a_writable_source"]
    assert log["seams"]["write_back_raises"]
    assert log["seams"]["project_from_source_refuses_a_writable_source"]
    assert log["projection_pass"]["work_items_upserted"] > 0


def test_a2_fails_when_the_read_only_source_forwards_writes(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_11, "ReadOnlySource", LeakyReadOnlySource)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("can reach 'upsert_task'" in f for f in outcome.findings)


def test_a2_fails_when_the_projection_writes_back(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_11, "PlaneProjection", ProjectionThatWritesBack)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("authoritative state changed across a projection pass" in f for f in outcome.findings)


def test_a2_fails_when_the_pass_publishes_nothing(ctx, gate, monkeypatch):
    """An inert pass makes 'unchanged' vacuous, so it must not pass."""
    monkeypatch.setattr(checks_d2_11, "PlaneProjection", ProjectionThatPublishesNothing)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("wrote nothing to Plane" in f for f in outcome.findings)


def test_a2_negative_control_moves_the_fingerprint(ctx, gate):
    control = run(ctx, gate, "A2").evidence["negative_control_transcript"]
    assert control["comparator_detects_the_change"]
    assert control["pass_was_not_inert"]
    assert control["probe"] and control["why"]


def test_a2_states_how_the_two_stores_are_modelled(ctx, gate):
    log = run(ctx, gate, "A2").evidence["gate_execution_log"]
    assert "not TerminusDB" in log["how_the_stores_are_modelled"]
    assert "MockTransport" in log["how_the_stores_are_modelled"]


# --- A3 --------------------------------------------------------------------


def test_a3_passes_against_the_real_guard(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["clean_subjects_publish"]["plane_payload"]["published"]
    assert log["clean_subjects_publish"]["rendered_projection"]["published"]
    assert log["clean_payload_size"]["work_items"] > 0
    assert log["leak_caught_before_the_first_network_call"]["refused"]
    assert log["leak_caught_before_the_first_network_call"]["network_calls_made"] == 0
    assert log["dashboard_layer_refuses_assertion_text"]["refused"]
    assert log["dashboard_layer_refuses_a_model_identity"]["refused"]


def test_a3_catches_every_class_the_gate_and_the_pack_name(ctx, gate):
    injections = run(ctx, gate, "A3").evidence["negative_control_transcript"]["injections"]
    assert set(injections) == {
        "holdout_assertion",
        "private_fixture",
        "mutant_source",
        "assertion_body_in_a_value",
        "real_model_identity",
    }
    assert all(record["caught"] for record in injections.values())


def test_a3_fails_when_the_guard_passes_everything(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_11, "assert_no_protected_content", guard_that_passes_everything)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("protected content not caught" in f for f in outcome.findings)


def test_a3_fails_when_the_guard_rejects_everything(ctx, gate, monkeypatch):
    """A scanner that refuses clean content is not a leak check.

    It would satisfy 'zero protected content' perfectly and publish nothing, so
    the clean arm has to be able to fail on its own.
    """
    monkeypatch.setattr(checks_d2_11, "assert_no_protected_content", guard_that_rejects_everything)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("rejected by the guard" in f for f in outcome.findings)


def test_a3_would_fail_if_the_clean_subjects_were_empty(ctx, gate, monkeypatch):
    """Passing a scan over nothing proves nothing, and must not read as green."""
    monkeypatch.setattr(
        checks_d2_11,
        "_seeded_control_plane",
        lambda repo_root: (ControlPlaneWithNothingInIt(), "EMPTY"),
    )
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("clean subjects are empty" in f for f in outcome.findings)


# --- A4 --------------------------------------------------------------------


def test_a4_passes_against_the_real_timing_record(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert len(log["timing_record_fields"]) == 10
    assert log["every_field_is_a_system_event_timestamp"]
    assert log["estimate_field_refused_by_the_schema"]["refused"]
    assert log["durations_computed"] > 0
    assert log["plane_yaml_worklog_policy"]["source"] == "system_events_only"
    assert log["plane_yaml_worklog_policy"]["agent_estimates_permitted"] is False


def test_a4_computes_the_durations_by_subtraction(ctx, gate):
    measurements = run(ctx, gate, "A4").evidence["gate_execution_log"]["measurements"]
    full = measurements["fully_recorded_task"]
    assert full["observed"]["queue_duration"] == 30.0
    assert full["observed"]["active_duration"] == 480.0
    assert full["observed"]["total_wall_clock"] == 720.0
    # The projection returns the seven keys it can derive; this check's own
    # subtraction covers all nine, and the two it adds are exactly the ones no
    # control-plane event can produce.
    recomputed = full["recomputed_by_this_check"]
    assert {key: recomputed[key] for key in full["observed"]} == full["observed"]
    assert set(recomputed) - set(full["observed"]) == {"model_call_duration", "tool_duration"}


def test_a4_returns_none_rather_than_a_guess_for_a_missing_timestamp(ctx, gate):
    measurements = run(ctx, gate, "A4").evidence["gate_execution_log"]["measurements"]
    empty = measurements["no_timestamps_at_all"]
    assert set(empty["keys_returning_none"]) == set(empty["worklog"])
    assert all(value is None for value in empty["observed"].values())
    # And the keys the control plane cannot derive at all, on a full record.
    full = measurements["fully_recorded_task"]
    assert full["worklog"]["model_call_duration"] is None
    assert full["worklog"]["tool_duration"] is None


def test_a4_fails_when_the_projection_guesses(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_11, "derived_durations", guessing_durations)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("a projection that guesses is a projection that lies" in f for f in outcome.findings)


def test_a4_fails_when_the_timing_record_accepts_an_estimate(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d2_11, "TimingRecord", TimingRecordWithAnEstimate)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("names an agent estimate" in f for f in outcome.findings)
    assert any("accepted an estimated_hours field" in f for f in outcome.findings)


def test_a4_negative_controls_fire_for_the_stated_reason(ctx, gate):
    control = run(ctx, gate, "A4").evidence["negative_control_transcript"]
    assert control["estimate_detector_fires"]
    assert control["guess_detector_fires"]
    assert any("estimated_hours" in f for f in control["estimate_detector_findings"])
    assert any("900.0" in f for f in control["guess_detector_findings"])


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_emits_the_evidence_the_gate_named(ctx, gate, assertion_id):
    outcome = run(ctx, gate, assertion_id)
    assert set(gate.evidence_required) <= set(outcome.evidence)
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4"])
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = run(ctx, gate, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


def test_no_check_touched_the_owners_pack(ctx, gate):
    """The pack is hash-locked owner data; these checks only read it."""
    manifest_before = InMemoryControlPlane().import_project(
        pack_root=str(ctx.repo_root / "project-pack"), requested_by="t", correlation_id="c"
    ).pack_manifest_hash
    for assertion_id in ("A1", "A2", "A3", "A4"):
        run(ctx, gate, assertion_id)
    manifest_after = InMemoryControlPlane().import_project(
        pack_root=str(ctx.repo_root / "project-pack"), requested_by="t", correlation_id="c"
    ).pack_manifest_hash
    assert manifest_after == manifest_before


def test_the_registered_gate_runs_green_with_its_evidence(monkeypatch):
    """The registration entries, exercised end to end through the runner.

    This is what merging :data:`CHECKS_D2_11` into ``CHECKS`` would buy: the gate
    reports EXECUTED rather than NOT_YET_EXECUTABLE, and it produces every
    artifact its own definition named.
    """
    for key, check in CHECKS_D2_11.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]
    assert result.evidence_missing == []
    assert result.executed_count == len(result.assertions) == 4
