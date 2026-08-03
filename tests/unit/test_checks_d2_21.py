"""GATE-D2-21's checks, and the proof that they can fail.

Contract Sections 19.1, 19.2, 19.5 · Section 18. A gate check that passes
against the real drift engine tells you very little on its own: the same green
would appear if the check compared a constant to itself. So every check here is
exercised twice -- once against the real engine and the real Section 19.5
boundary, and once against a deliberately broken one where the property the
assertion names is false. The second run is what gives the first its meaning.

Each broken subject is broken in exactly one way, and the way is named:

* an engine blind to one finding type -- the detector that never fires (A1, A2,
  A3);
* an engine that flags every task or every path -- the detector that always
  fires, which satisfies every one of these assertions read literally and is
  worthless (A1, A3);
* an engine that records a requirement weakening and lets it through anyway
  (A2), which is the difference between ``detected`` and ``detected_and_blocked``
  and the only reason A2 is not a duplicate of A1;
* a classifier that blocks everything, and one that demotes everything (A4);
* a schema assert that never reports a violation, and one that always does (A5).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from drift import security
from drift.engine import DriftEngine, DriftReport, DriftScanInput, Finding
from evaluation import checks_d2_21
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d2_21 import CHECKS_D2_21
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import DriftFinding, Verdict

GATE_ID = "GATE-D2-21"
ASSERTION_IDS = ["A1", "A2", "A3", "A4", "A5"]


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context over the real pack.

    The candidate commit is a stand-in because these tests are about the checks
    and not about the binding; the gate-runner test at the end uses real HEAD.
    """
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D2_21[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


# --- broken subjects -------------------------------------------------------


class _BlindEngine(DriftEngine):
    """An engine whose detector for one finding type never fires."""

    BLIND_TO: str = ""

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = super().scan(scan_input)
        report.findings = [f for f in report.findings if f.finding != self.BLIND_TO]
        return report


class EngineBlindToUnlinkedTasks(_BlindEngine):
    BLIND_TO = str(DriftFinding.UNLINKED_TASK)


class EngineBlindToRequirementWeakening(_BlindEngine):
    BLIND_TO = str(DriftFinding.REQUIREMENT_WEAKENING)


class EngineBlindToPathViolations(_BlindEngine):
    BLIND_TO = str(DriftFinding.OUTSIDE_ALLOWED_PATHS)


class EngineThatFlagsEveryTask(DriftEngine):
    """Reports every active task unlinked. The shape of a detector left on."""

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = super().scan(scan_input)
        seen = {f.subject for f in report.of_type(DriftFinding.UNLINKED_TASK)}
        for task in scan_input.active_tasks:
            if task.task_id not in seen:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNLINKED_TASK),
                        subject=task.task_id,
                        detail="active task links to no compiled requirement",
                    )
                )
        return report


class EngineThatRejectsEveryPath(DriftEngine):
    """Rejects every changed path it is shown, permitted or not."""

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = super().scan(scan_input)
        for task in scan_input.active_tasks:
            for path in task.changed_paths:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                        subject=task.task_id,
                        detail=f"changed path {path!r} is outside the work unit's allowed paths",
                        evidence=(path,),
                    )
                )
        return report


class EngineThatCallsEveryPathViolationTheSameThing(DriftEngine):
    """Collapses PROTECTED_ASSET_ACCESS into OUTSIDE_ALLOWED_PATHS.

    Section 19.2's vocabulary is closed; an engine that stamps one label on
    every path it dislikes still 'rejects changes outside the allowed paths'.
    """

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = super().scan(scan_input)
        report.findings = [
            dataclasses.replace(
                f,
                finding=str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                detail=f.detail.replace("reaches a sealed asset", "is outside the work unit's allowed paths"),
            )
            if f.finding == str(DriftFinding.PROTECTED_ASSET_ACCESS)
            else f
            for f in report.findings
        ]
        return report


class EngineThatOnlyObservesRequirementWeakening(DriftEngine):
    """Notices every weakening and blocks none of them.

    The precise failure A2 exists for: the finding is in the report, the report
    is green, ``unresolved_scope_drift`` is zero, and the merge proceeds.
    """

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = super().scan(scan_input)
        report.findings = [
            dataclasses.replace(f, blocks=False)
            if f.finding == str(DriftFinding.REQUIREMENT_WEAKENING)
            else f
            for f in report.findings
        ]
        return report


def _classification(finding: security.SecurityFinding, *, blocks: bool) -> Any:
    if blocks:
        return security.SecurityClassification(
            finding_id=finding.finding_id,
            blocks=True,
            finding_type=None,
            satisfied=security.REQUIRED_CONDITIONS,
            admitted_refs=tuple(finding.mapped_refs),
            rationale="broken classifier: everything is in scope",
        )
    return security.SecurityClassification(
        finding_id=finding.finding_id,
        blocks=False,
        finding_type=str(DriftFinding.OUT_OF_SCOPE_OBSERVATION),
        satisfied=(),
        unsatisfied=security.REQUIRED_CONDITIONS,
        rationale="broken classifier: everything is out of scope",
    )


@pytest.fixture
def swap_engine(monkeypatch):
    """Replace the engine class the checks construct, and drop the cache.

    ``checks_d2_21._engine`` is memoised per repo root, so a substitution that
    did not clear it would silently exercise the real engine and every test
    below would pass for the wrong reason.
    """

    def _swap(cls: type[DriftEngine]) -> None:
        checks_d2_21._engine.cache_clear()
        monkeypatch.setattr(checks_d2_21, "DriftEngine", cls)

    yield _swap
    checks_d2_21._engine.cache_clear()


# --- the registry ----------------------------------------------------------


def test_the_registry_covers_every_assertion_the_pack_declares(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D2_21 if gid == GATE_ID}
    assert registered == declared == set(ASSERTION_IDS)
    assert all(gid == GATE_ID for gid, _ in CHECKS_D2_21)


def test_the_module_does_not_import_checks_at_module_scope():
    """The circular-import guard, asserted rather than trusted to a comment.

    ``evaluation.checks`` imports this module to register it. Importing back at
    module scope makes which side breaks depend on which one Python loads
    first, so the same code works under the gate runner and explodes under
    pytest. ``ok`` and ``bad`` must therefore resolve inside the call.
    """
    source = (checks_d2_21.__file__ or "").replace(".pyc", ".py")
    with open(source) as handle:
        lines = handle.read().splitlines()
    module_scope_imports = [
        line
        for line in lines
        if line.startswith(("import ", "from ")) and "evaluation.checks" in line
    ]
    assert module_scope_imports == []


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_drift_engine(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    empty_arm = log["arms"]["declares_no_requirement_ids"]
    phantom_arm = log["arms"]["declares_a_requirement_the_catalog_does_not_contain"]
    assert empty_arm["subjects"] == [log["injection_target"]]
    assert phantom_arm["subjects"] == [log["injection_target"]]
    assert all(f["blocks"] for f in phantom_arm["unlinked_task_findings"])
    assert "not in the compiled catalog" in phantom_arm["unlinked_task_findings"][0]["detail"]


def test_a1_negative_control_is_silence_on_the_whole_compiled_plan(ctx, gate):
    control = run(ctx, gate, "A1").evidence["negative_control_transcript"]
    sweep = control["all_compiled_tasks_presented_as_active"]
    assert sweep["tasks_scanned"] > 1
    assert sweep["unlinked_task_findings"] == []
    assert control["clean_task"]["unlinked_task_findings"] == []
    assert control["clean_task"]["terminal_state"] == "RUNNING"


def test_a1_fails_when_the_detector_never_fires(ctx, gate, swap_engine):
    swap_engine(EngineBlindToUnlinkedTasks)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("produced no UNLINKED_TASK finding" in f for f in outcome.findings)


def test_a1_fails_when_the_detector_flags_every_task(ctx, gate, swap_engine):
    """An engine that flags everything satisfies A1's wording and is broken."""
    swap_engine(EngineThatFlagsEveryTask)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control failed" in f for f in outcome.findings)
    assert any("is not a drift detector" in f for f in outcome.findings)


# --- A2 --------------------------------------------------------------------


def test_a2_passes_against_the_real_drift_engine(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["victim_gate_blocking_in_the_contract"] is True
    for arm in log["arms"].values():
        assert arm["matching_findings"], arm
        assert all(arm["blocks"])
        assert arm["terminal_state"] == "FAILED_CONTRACT"
        assert arm["unresolved_scope_drift"] >= 1


def test_a2_covers_all_three_ways_a_requirement_is_weakened(ctx, gate):
    arms = run(ctx, gate, "A2").evidence["gate_execution_log"]["arms"]
    assert set(arms) == {
        "blocking_gate_observed_as_non_blocking",
        "assertion_removed_from_the_gate",
        "gate_absent_from_the_observed_set",
    }
    subject = arms["assertion_removed_from_the_gate"]["expected_subject"]
    assert "." in subject, "the assertion arm must name gate.assertion, not just the gate"


def test_a2_fails_when_weakening_is_invisible(ctx, gate, swap_engine):
    swap_engine(EngineBlindToRequirementWeakening)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("no REQUIREMENT_WEAKENING finding names" in f for f in outcome.findings)


def test_a2_fails_when_weakening_is_detected_but_not_blocked(ctx, gate, swap_engine):
    """``detected_and_blocked`` is two claims, and this engine satisfies one.

    Without this arm A2 would be a second copy of A1: the finding is present,
    the report is green, and the merge proceeds over a gate that stopped
    blocking.
    """
    swap_engine(EngineThatOnlyObservesRequirementWeakening)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("recorded as a non-blocking observation" in f for f in outcome.findings)
    assert any("detected but not blocked" in f for f in outcome.findings)
    assert any("unresolved_scope_drift is 0" in f for f in outcome.findings)


def test_a2_negative_control_is_the_unchanged_gate_set(ctx, gate):
    control = run(ctx, gate, "A2").evidence["negative_control_transcript"]
    assert control["gates_observed"] > 1
    assert control["requirement_weakening_findings"] == []
    assert control["blocking_findings"] == []
    assert control["terminal_state"] == "RUNNING"


# --- A3 --------------------------------------------------------------------


def test_a3_passes_against_the_real_drift_engine(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    ungoverned = log["arms"]["matches_none_of_the_allowed_path_patterns"]
    assert ungoverned["terminal_state"] == "FAILED_CONTRACT"
    assert all(f["blocks"] for f in ungoverned["findings_of_the_expected_type"])
    assert ungoverned["changed_path"] in ungoverned["findings_of_the_expected_type"][0]["evidence"]
    # Every prohibited pattern the work unit declares is probed, not just one.
    assert sum(1 for name in log["arms"] if name.startswith("prohibited_pattern ")) == len(
        log["prohibited_paths"]
    )


def test_a3_types_a_sealed_path_as_protected_asset_access(ctx, gate):
    arms = run(ctx, gate, "A3").evidence["gate_execution_log"]["arms"]
    sealed_arm = arms["reaches_a_sealed_asset"]
    assert sealed_arm["expected_finding"] == str(DriftFinding.PROTECTED_ASSET_ACCESS)
    assert sealed_arm["findings_of_the_expected_type"]
    assert sealed_arm["other_path_finding_types_produced"] == []


def test_a3_fails_when_the_path_policy_never_fires(ctx, gate, swap_engine):
    swap_engine(EngineBlindToPathViolations)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("produced no OUTSIDE_ALLOWED_PATHS finding" in f for f in outcome.findings)


def test_a3_fails_when_every_reported_path_is_rejected(ctx, gate, swap_engine):
    """A blanket refusal is not a path policy, and must not read as one."""
    swap_engine(EngineThatRejectsEveryPath)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any(
        "which the work unit's own allowed paths permit, was reported OUTSIDE_ALLOWED_PATHS" in f
        for f in outcome.findings
    )


def test_a3_fails_when_a_sealed_path_is_typed_as_an_ordinary_path_violation(
    ctx, gate, swap_engine
):
    swap_engine(EngineThatCallsEveryPathViolationTheSameThing)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("produced no PROTECTED_ASSET_ACCESS finding" in f for f in outcome.findings)


def test_a3_negative_control_is_a_permitted_change(ctx, gate):
    control = run(ctx, gate, "A3").evidence["negative_control_transcript"]
    assert control["outside_allowed_paths_findings"] == []
    assert control["protected_asset_access_findings"] == []
    assert control["terminal_state"] == "RUNNING"


def test_a3_states_that_no_file_is_written_or_reverted(ctx, gate):
    log = run(ctx, gate, "A3").evidence["gate_execution_log"]
    assert "not a filesystem interceptor" in log["what_rejected_means_here"]


# --- A4 --------------------------------------------------------------------


def test_a4_passes_against_the_real_security_boundary(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    arms = outcome.evidence["gate_execution_log"]["arms"]
    for arm in arms.values():
        assert arm["classification"]["blocks"] is False
        assert arm["classification"]["finding_type"] == str(DriftFinding.OUT_OF_SCOPE_OBSERVATION)
        assert arm["blocking_count"] == 0
        assert arm["unresolved_scope_drift"] == 0
        assert arm["terminal_state"] == "RUNNING"


def test_a4_refuses_a_finding_that_mints_its_own_authority(ctx, gate):
    arm = run(ctx, gate, "A4").evidence["gate_execution_log"]["arms"][
        "maps_to_references_the_contract_does_not_contain"
    ]
    assert arm["classification"]["admitted_refs"] == []
    assert "not in the approved set" in arm["classification"]["rationale"]


def test_a4_negative_controls_block_and_refuse_expansion(ctx, gate):
    control = run(ctx, gate, "A4").evidence["negative_control_transcript"]
    assert control["in_scope_finding"]["classification"]["blocks"] is True
    assert control["in_scope_finding"]["terminal_state"] == "FAILED_CONTRACT"
    assert control["observation_that_proposes_work"]["expansion_findings"]
    assert control["observation_that_proposes_work"]["blocking_count"] >= 1


def test_a4_fails_when_the_classifier_blocks_everything(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        security, "classify", lambda finding, approved: _classification(finding, blocks=True)
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("classified as blocking" in f for f in outcome.findings)


def test_a4_fails_when_the_classifier_demotes_everything(ctx, gate, monkeypatch):
    """Demoting every finding satisfies A4's words and disarms security review."""
    monkeypatch.setattr(
        security, "classify", lambda finding, approved: _classification(finding, blocks=False)
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control failed" in f and "did not block" in f for f in outcome.findings)


# --- A5 --------------------------------------------------------------------


def test_a5_passes_against_the_real_schema_assert(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["complete_finding"]["blocking_schema_violations"] == []
    assert len(log["complete_finding"]["admitted_to_blocking"]) == 1
    assert log["whitespace_remediation"]["blocks"] is False


def test_a5_carries_one_negative_control_per_section_19_5_condition(ctx, gate):
    control = run(ctx, gate, "A5").evidence["negative_control_transcript"]
    assert set(control["controls"]) == set(security.REQUIRED_CONDITIONS)
    for condition, record in control["controls"].items():
        assert record["blocking_schema_violations"] == [condition]
        assert record["admitted_to_blocking"] == []
        assert len(record["demoted_to_observation"]) == 1


def test_a5_accepts_evidence_or_a_probe_but_not_neither(ctx, gate):
    log = run(ctx, gate, "A5").evidence["gate_execution_log"]
    for record in log["evidence_or_probe_disjunction"].values():
        assert record["blocking_schema_violations"] == []
        assert record["blocks"] is True
    control = run(ctx, gate, "A5").evidence["negative_control_transcript"]["controls"]
    assert control[security.CONDITION_EVIDENCE]["blocking_schema_violations"] == [
        security.CONDITION_EVIDENCE
    ]


def test_a5_fails_when_the_schema_assert_never_reports_a_violation(ctx, gate, monkeypatch):
    monkeypatch.setattr(security, "blocking_schema_violations", lambda finding: [])
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("must name exactly the condition that is absent" in f for f in outcome.findings)


def test_a5_fails_when_a_complete_finding_is_reported_incomplete(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        security, "blocking_schema_violations", lambda finding: list(security.REQUIRED_CONDITIONS)
    )
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("was reported to be missing" in f for f in outcome.findings)


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", ASSERTION_IDS)
def test_every_check_emits_the_evidence_the_gate_named(ctx, gate, assertion_id):
    outcome = run(ctx, gate, assertion_id)
    assert set(gate.evidence_required) <= set(outcome.evidence)
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["gate_source_hash"] == gate.source_hash
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", ASSERTION_IDS)
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = run(ctx, gate, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


@pytest.mark.parametrize("assertion_id", ASSERTION_IDS)
def test_every_check_reports_the_method_and_expected_value_the_pack_declares(
    ctx, gate, assertion_id
):
    spec = assertion(gate, assertion_id)
    log = run(ctx, gate, assertion_id).evidence["gate_execution_log"]
    assert log["check"] == spec.method
    assert log["expected"] == spec.expected
    assert log["failure_state"] == spec.failure_state


def test_the_registered_gate_runs_green_with_its_evidence(monkeypatch):
    """The registration entries, exercised end to end through the runner.

    This is what merging :data:`CHECKS_D2_21` into ``CHECKS`` buys: the gate
    reports EXECUTED rather than NOT_YET_EXECUTABLE, and it produces every
    artifact its own definition named.
    """
    for key, check in CHECKS_D2_21.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]
    assert result.evidence_missing == []
    assert result.executed_count == len(result.assertions) == 5
