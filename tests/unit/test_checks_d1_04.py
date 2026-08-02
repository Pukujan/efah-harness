"""GATE-D1-04's checks, and the proof that they can fail.

Contract Sections 10.3, 10.4, 10.6 · Section 18. A gate check that is green
against a working system tells you nothing on its own -- the same green appears
if the check compares a constant to itself. So every check in
:mod:`evaluation.checks_d1_04` is exercised twice here: once against the real
runtime, and once against a subject that is broken in exactly one identifiable
way, with the finding it must produce named.

The broken subjects, and what each one stands for:

* a runtime whose ``resume`` quietly starts the run over (A2) -- the defect the
  whole gate exists to catch, and the one that is invisible to any probe that
  only asks whether the run finished;
* an adapter without the Section 10.4 enforcing saver (A3) -- the shape of a
  build where the twelve-field rule was documented rather than enforced;
* a source tree that really does import ``AsyncSqliteSaver`` outside the adapter
  (A5), scanned by the same scanner over a real directory;
* and, for each check, an arm that leaves the *negative control* inert, because
  a control that cannot fire is a check that cannot fail.

One honest limit is recorded rather than papered over. A1's verdict comes from a
subprocess pytest run over the pinned probe, so the test below that makes A1 fail
does it by pointing the selector at a test that does not exist. That proves the
check reports the subprocess's verdict instead of a constant; it does not
simulate a checkpointer that fails to resume, which would need a second copy of
the probe free to drift from the first. The property itself is defended by A1's
own no-store control and by A2's counters, both of which are exercised here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from evaluation import checks_d1_04
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d1_04 import CHECKS_D1_04
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict
from workflows.checkpoint import SqliteCheckpointAdapter
from workflows.runtime import WorkflowRuntime

GATE_ID = "GATE-D1-04"
REGISTERED = ("A1", "A2", "A3", "A5")
#: A4 needs a live TerminusDB to answer its second half. See CHECKS_D1_04.
UNREGISTERED = ("A4",)

#: Where each check hangs the evidence its gate named.
ARTIFACT = {
    "A1": "kill_restart_transcript",
    "A2": "node_execution_counters",
    "A3": "checkpoint_field_dump",
    "A5": "import_boundary_report",
}


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context. The commit is a stand-in because these tests are about
    the checks; the gate-runner test at the end binds to the real HEAD."""
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D1_04[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


@pytest.fixture(scope="module")
def passing(ctx: GateContext, gate: GateSpec) -> dict[str, Any]:
    """Every registered check, run once against the real system.

    Module-scoped because A1 and A2 each kill a real child process, and paying
    for that once per assertion is the difference between a fast unit run and a
    slow one.
    """
    return {assertion_id: run(ctx, gate, assertion_id) for assertion_id in REGISTERED}


# --- broken subjects -------------------------------------------------------


class RuntimeThatRestartsInsteadOfResuming(WorkflowRuntime):
    """``resume`` that starts the run over on a clean thread.

    The failure GATE-D1-04 A2 exists to catch, and the one that looks fine from
    the outside: the run still finishes, the state is still complete, and the
    only trace is that work already done was done again.
    """

    async def resume(self, graph_id: str, *, thread_id: str, attempt: int = 2):  # type: ignore[override]
        state = self.new_state(graph_id=graph_id, work_unit_id=checks_d1_04.WORK_UNIT_ID)
        return await self.run(
            graph_id, state, thread_id=f"{thread_id}-restarted", attempt=attempt, resumed=True
        )


class AdapterWithoutSection104Enforcement(SqliteCheckpointAdapter):
    """The Section 10.3 adapter with its enforcing saver swapped out.

    Everything else is real -- same store, same strict serializer, same read
    side. Only the write-time Section 10.4 assertion is gone, which is exactly
    what a build that documented the twelve fields instead of enforcing them
    would look like.
    """

    def saver(self):  # type: ignore[override]
        enforcing = super().saver()
        return AsyncSqliteSaver(enforcing.conn, serde=enforcing.serde)


# --- the registry ----------------------------------------------------------


def test_the_registry_holds_exactly_the_assertions_that_are_executable(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D1_04 if gid == GATE_ID}
    assert registered == set(REGISTERED)
    assert declared - registered == set(UNREGISTERED)
    assert all(gid == GATE_ID for gid, _ in CHECKS_D1_04)


def test_a4_is_left_unregistered_rather_than_passed_on_its_visible_half():
    """A4's method is ``delete_checkpoints_then_query_terminusdb``.

    The delete is trivial here -- ``destroy()`` exists and ``is_authoritative``
    is ``False`` -- and the query is not possible without a live TerminusDB.
    Registering a check that performed the delete and asserted the adapter's own
    flag would report that project truth survived without asking the system that
    holds it.
    """
    assert (GATE_ID, "A4") not in CHECKS_D1_04


def test_merged_entries_resolve_to_this_modules_checks():
    """``checks.py`` merges the per-gate maps; whether it has merged this one
    yet is not this module's business. What must hold either way is that a key
    present in the registry resolves to *this* module's function -- a duplicate
    registration elsewhere wins silently under ``dict.update``."""
    for key, check in CHECKS_D1_04.items():
        if key in CHECKS:
            assert CHECKS[key] is check, f"{key} resolves to a different check than this module's"


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_kill_and_restart_probe(passing):
    outcome = passing["A1"]
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    transcript = outcome.evidence["kill_restart_transcript"]
    assert transcript["passed"] is True
    assert len(transcript["selectors"]) == 2
    assert "SIGKILL" in transcript["signal_used"]
    assert transcript["negative_control"]["detector_fires"] is True


def test_a1_negative_control_shows_a_storeless_resume_cannot_continue(passing):
    control = passing["A1"].evidence["kill_restart_transcript"]["negative_control"]
    assert control["refused"] is True
    assert sum(control["nodes_executed"].values()) == 0
    assert control["produced_a_result"] is False


def test_a1_fails_when_the_pinned_probe_does_not_pass(ctx, gate, monkeypatch):
    """The verdict is the subprocess's, not a constant.

    Selecting a test that does not exist makes pytest exit non-zero, and the
    check must report that rather than its own opinion of the runtime.
    """
    monkeypatch.setattr(checks_d1_04, "A1_TESTS", ("test_this_probe_does_not_exist",))
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("kill/restart probe failed" in f for f in outcome.findings)


def test_a1_fails_when_a_storeless_process_could_still_continue(ctx, gate, monkeypatch):
    """The control is load-bearing: without it, 'the run continued' would be a
    statement about the script rather than about the checkpoint."""

    def inert_control(module, repo_root, workdir):
        return {
            "probe": "inert",
            "why": "inert",
            "returncode": 0,
            "refused": False,
            "nodes_executed": {"durable_branch": 1},
            "produced_a_result": True,
            "detector_fires": False,
            "error": "",
        }

    monkeypatch.setattr(checks_d1_04, "_resume_without_a_store", inert_control)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in f for f in outcome.findings)


# --- A2 --------------------------------------------------------------------


def test_a2_passes_with_a_completed_node_rerun_count_of_zero(passing):
    outcome = passing["A2"]
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    counters = outcome.evidence["node_execution_counters"]
    kill_arm = counters["process_kill_arm"]
    assert kill_arm["killed_by_sigkill"] is True
    assert kill_arm["write_became_durable_before_the_kill"] is True
    assert kill_arm["completed_node_rerun_count"] == 0
    assert kill_arm["counts_after_the_resume"]["durable_branch"] == 1
    # The decisive comparison: the output exists although its producer did not
    # run again, so it came out of the checkpoint.
    assert kill_arm["output_of_the_completed_node_survived_without_re_execution"] is True
    assert counters["shipped_graph_arm"]["completed_node_rerun_count"] == 0
    assert counters["shipped_graph_arm"]["lease_generations"] == [1]


def test_a2_negative_control_counts_a_restart_as_a_rerun(passing):
    control = passing["A2"].evidence["node_execution_counters"]["negative_control"]
    assert control["detector_fires"] is True
    assert control["completed_node_rerun_count"] > 0
    assert control["completed_before_the_interruption"] == ["claim_lease"]


def test_a2_fails_when_resume_silently_restarts_the_run(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_04, "WorkflowRuntime", RuntimeThatRestartsInsteadOfResuming)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("ran again after the resume" in f for f in outcome.findings)


def test_a2_fails_when_the_restart_control_reports_no_rerun(ctx, gate, monkeypatch):
    """A counter that never increments satisfies 'zero reruns' perfectly."""
    original = checks_d1_04._observer_rerun_probe

    async def blunted(repo_root, workdir, *, restart_instead_of_resume):
        record = await original(
            repo_root, workdir, restart_instead_of_resume=restart_instead_of_resume
        )
        if restart_instead_of_resume:
            record["completed_node_rerun_count"] = 0
        return record

    monkeypatch.setattr(checks_d1_04, "_observer_rerun_probe", blunted)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("not measuring re-execution" in f for f in outcome.findings)


# --- A3 --------------------------------------------------------------------


def test_a3_passes_with_all_twelve_fields_on_every_checkpoint(passing):
    outcome = passing["A3"]
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    dump = outcome.evidence["checkpoint_field_dump"]
    assert dump["required_field_count"] == 12
    assert dump["tuple_and_model_agree"] is True
    assert dump["state_carrying_checkpoints"] > 0
    assert dump["checkpoints_missing_a_required_field"] == []
    assert len(dump["dump"]) == dump["state_carrying_checkpoints"]


def test_a3_negative_controls_are_refused_at_write_time(passing):
    control = passing["A3"].evidence["checkpoint_field_dump"]["negative_control"]
    assert control["detector_fires"] is True
    for label in ("field_absent", "field_present_but_null"):
        assert control[label]["refused"] is True
        assert control[label]["missing"] == ["terminus_commit"]
        # Nothing incomplete was persisted: the refusal happens before the write.
        assert control[label]["state_carrying_checkpoints_written"] == 0


def test_a3_fails_when_the_adapter_stops_enforcing_section_10_4(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d1_04, "SqliteCheckpointAdapter", AdapterWithoutSection104Enforcement
    )
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("did not fire" in f for f in outcome.findings)


def test_a3_fails_if_the_required_field_list_stops_being_twelve(ctx, gate, monkeypatch):
    """The dump is only worth reading against the whole list.

    Shortening the closed tuple would make a complete dump of an incomplete
    rule, which is the quietest possible way to weaken this gate.
    """
    monkeypatch.setattr(
        checks_d1_04, "REQUIRED_CHECKPOINT_FIELDS", checks_d1_04.REQUIRED_CHECKPOINT_FIELDS[:-1]
    )
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("names twelve checkpoint references" in f for f in outcome.findings)


# --- A5 --------------------------------------------------------------------


def test_a5_passes_and_the_scanner_can_see_the_adapter(passing):
    outcome = passing["A5"]
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = outcome.evidence["import_boundary_report"]
    assert report["offenders"] == []
    assert report["files_scanned"] > 0
    assert report["files_that_would_not_parse"] == []
    control = report["negative_control"]
    assert control["detector_fires"] is True
    assert any("AsyncSqliteSaver" in name for name in control["imports_found"])


def test_a5_fails_on_a_tree_that_really_reaches_the_checkpointer(gates, gate, tmp_path: Path):
    """A real scan over a real directory, not a stubbed predicate."""
    real_adapter = Path(checks_d1_04.__file__).resolve().parents[1] / "workflows" / "checkpoint.py"
    fake_src = tmp_path / "src"
    (fake_src / "workflows").mkdir(parents=True)
    shutil.copy(real_adapter, fake_src / "workflows" / "checkpoint.py")
    (fake_src / "sneaky_runtime.py").write_text(
        "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver\n\nSAVER = AsyncSqliteSaver\n"
    )
    ctx = GateContext(
        binding=CandidateBinding(commit_sha="b" * 40), gates=gates, repo_root=tmp_path
    )
    outcome = CHECKS_D1_04[(GATE_ID, "A5")](ctx, gate, assertion(gate, "A5"))
    assert outcome.status is AssertionStatus.FAIL
    assert any("sneaky_runtime.py reaches the checkpointer directly" in f for f in outcome.findings)


def test_a5_fails_when_the_scanner_matches_nothing(ctx, gate, monkeypatch):
    """Zero offenders is what a scanner that matches nothing reports too."""
    monkeypatch.setattr(checks_d1_04, "CHECKPOINTER_IMPORT_ROOTS", ())
    monkeypatch.setattr(checks_d1_04, "CHECKPOINTER_SYMBOLS", ())
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in f for f in outcome.findings)


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", REGISTERED)
def test_every_check_emits_its_named_artifact_bound_to_the_candidate(passing, ctx, assertion_id):
    outcome = passing[assertion_id]
    assert ARTIFACT[assertion_id] in outcome.evidence
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", REGISTERED)
def test_every_check_carries_a_negative_control_with_a_stated_reason(passing, assertion_id):
    control = passing[assertion_id].evidence[ARTIFACT[assertion_id]]["negative_control"]
    assert control["probe"]
    assert control["why"]
    assert control["detector_fires"] is True


def test_the_gate_named_evidence_this_workstream_produces(passing, gate: GateSpec):
    """Three of the gate's four named artifacts are produced here.

    ``post_delete_terminusdb_query_result`` belongs to A4 and is not produced,
    which is why the gate below reports UNVERIFIABLE rather than PASS. Recording
    that here keeps the gap visible instead of leaving it to be discovered by
    whoever next reads a summary.
    """
    produced = {name for outcome in passing.values() for name in outcome.evidence}
    assert {"kill_restart_transcript", "node_execution_counters", "checkpoint_field_dump"} <= produced
    assert "post_delete_terminusdb_query_result" not in produced
    assert set(gate.evidence_required) - produced == {"post_delete_terminusdb_query_result"}


def test_the_registered_checks_drive_the_gate_runner(monkeypatch):
    """End to end through the runner, against the real HEAD.

    The gate is PARTIALLY_EXECUTABLE and therefore UNVERIFIABLE, and that is the
    correct report: four assertions execute and pass, A4 has no check, and the
    runner refuses to call a gate PASS on the assertions that happened to be
    implemented.
    """
    for key, check in CHECKS_D1_04.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]

    assert result.executability is Executability.PARTIALLY_EXECUTABLE
    assert result.verdict is Verdict.UNVERIFIABLE
    assert result.failed == []
    assert result.executed_count == len(REGISTERED)
    statuses = {a.assertion_id: a.status for a in result.assertions}
    assert all(statuses[aid] is AssertionStatus.PASS for aid in REGISTERED)
    assert statuses["A4"] is AssertionStatus.NOT_IMPLEMENTED
    assert result.evidence_missing == ["post_delete_terminusdb_query_result"]
