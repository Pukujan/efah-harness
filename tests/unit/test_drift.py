"""Drift engine, security-scope boundary, and contract re-review.

Contract Sections 19.1, 19.2, 19.3, 19.4, 19.5.
Acceptance gates: GATE-D2-21 (scope drift and security expansion blocked) and
GATE-D2-22 (periodic and event-triggered contract review).

Every one of the thirteen Section 19.2 finding types gets a negative control: a
detector that has never been made to fire is not evidence of anything. The
security tests additionally prove the *inverse* property GATE-D2-21 A4 cares
about -- that an out-of-scope finding is demonstrably rejected rather than
quietly turned into work.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from contracts.compiler import compile_pack
from drift import security
from drift.engine import (
    ActiveTask,
    ArtifactClaim,
    DriftEngine,
    DriftScanInput,
    ReleaseContents,
    EvaluationClaim,
)
from drift.review import (
    DEFAULT_INTERVAL_MATERIAL_PHASES,
    ContractReviewScheduler,
    ReviewTrigger,
)
from governance.envelope import CONTRACT_VERSION
from governance.states import ContractReviewOutcome, DriftFinding, ProjectState, TaskState
from integrations.pack import load_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "project-pack"


@functools.lru_cache(maxsize=1)
def compiled():
    return compile_pack(load_pack(PACK_ROOT), repo_root=REPO_ROOT)


@functools.lru_cache(maxsize=1)
def engine():
    return DriftEngine(compiled())


def clean_task(**overrides):
    """An active task that matches the compiled plan exactly."""
    project = compiled()
    task_id = "TSK-001"
    task = project.tasks[task_id]
    defaults = dict(
        task_id=task_id,
        title=task["title"],
        requirement_ids=tuple(task["requirement_ids"]),
        contract_version=CONTRACT_VERSION,
        state=str(TaskState.RUNNING),
        changed_paths=("src/contracts/compiler.py",),
        allowed_paths=("src/**", "tests/**"),
        prohibited_paths=tuple(task["prohibited_paths"]),
    )
    defaults.update(overrides)
    return ActiveTask(**defaults)


def scan(**kwargs):
    return engine().scan(DriftScanInput(compiled=compiled(), **kwargs))


# --------------------------------------------------------------------------
# Section 19.1: the comparison runs over every input class


def test_scan_compares_every_section_19_1_input_class():
    report = scan(
        observed_gates=compiled().gates,
        active_tasks=[clean_task()],
        artifacts=[ArtifactClaim(artifact_id="ART-1", produced_by_task="TSK-001")],
        test_claims=[EvaluationClaim(gate_id="GATE-D1-03", evidence=("pytest.xml",), oracle_version="1.0.0")],
        release=ReleaseContents(
            commit="abc123",
            task_ids=("TSK-001",),
            gate_results={g: "PASS" for g, b in compiled().gates.items() if b.get("blocking")},
        ),
    )
    assert set(report.comparisons) == {
        "compiled_requirements",
        "compiled_tasks",
        "observed_gates",
        "active_tasks",
        "artifacts",
        "test_claims",
        "release_tasks",
        "security_findings",
    }
    assert report.comparisons["compiled_requirements"] > 0


def test_a_conformant_state_produces_no_blocking_findings():
    report = scan(
        observed_gates=compiled().gates,
        active_tasks=[clean_task()],
        test_claims=[EvaluationClaim(gate_id="GATE-D1-03", evidence=("pytest.xml",))],
    )
    assert report.blocking == [], [f.as_body() for f in report.blocking]
    assert report.unresolved_scope_drift == 0
    assert report.terminal_state is ProjectState.RUNNING


# --------------------------------------------------------------------------
# Section 19.2: one negative control per finding type


def test_unlinked_task_is_detected():
    report = scan(active_tasks=[clean_task(requirement_ids=())])
    assert report.of_type(DriftFinding.UNLINKED_TASK)


def test_a_task_citing_a_requirement_that_does_not_exist_is_also_unlinked():
    report = scan(active_tasks=[clean_task(requirement_ids=("REQ-INVENTED-999",))])
    findings = report.of_type(DriftFinding.UNLINKED_TASK)
    assert findings and "not in the compiled catalog" in findings[0].detail


def test_unapproved_scope_expansion_is_detected():
    report = scan(active_tasks=[clean_task(task_id="TSK-SMUGGLED")])
    assert report.of_type(DriftFinding.UNAPPROVED_SCOPE_EXPANSION)


def test_requirement_weakening_is_detected_when_a_gate_stops_blocking():
    gates = {k: dict(v) for k, v in compiled().gates.items()}
    gates["GATE-D1-10"]["blocking"] = False
    report = scan(observed_gates=gates)
    findings = report.of_type(DriftFinding.REQUIREMENT_WEAKENING)
    assert any(f.subject == "GATE-D1-10" for f in findings)


def test_requirement_weakening_is_detected_when_an_assertion_disappears():
    gates = {k: dict(v) for k, v in compiled().gates.items()}
    gates["GATE-D1-03"] = dict(gates["GATE-D1-03"])
    gates["GATE-D1-03"]["assertions"] = gates["GATE-D1-03"]["assertions"][:-1]
    report = scan(observed_gates=gates)
    assert any(f.subject == "GATE-D1-03.A5" for f in report.of_type(DriftFinding.REQUIREMENT_WEAKENING))


def test_redefined_success_is_detected_when_an_expected_value_changes():
    gates = {k: dict(v) for k, v in compiled().gates.items()}
    gates["GATE-D1-03"] = dict(gates["GATE-D1-03"])
    assertions = [dict(a) for a in gates["GATE-D1-03"]["assertions"]]
    assertions[3]["expected"] = "path_length >= 0"
    gates["GATE-D1-03"]["assertions"] = assertions
    report = scan(observed_gates=gates)
    assert any(f.subject == "GATE-D1-03.A4" for f in report.of_type(DriftFinding.REDEFINED_SUCCESS))


def test_redefined_success_is_detected_when_a_claim_ran_against_a_different_assertion_hash():
    report = scan(
        observed_gate_hashes={"GATE-D1-03": "sha256:pinned"},
        test_claims=[EvaluationClaim(gate_id="GATE-D1-03", assertion_hash="sha256:something-else", evidence=("x",))],
    )
    assert report.of_type(DriftFinding.REDEFINED_SUCCESS)


def test_a_pass_claimed_with_no_evidence_is_redefined_success():
    report = scan(test_claims=[EvaluationClaim(gate_id="GATE-D1-03", verdict="PASS")])
    findings = report.of_type(DriftFinding.REDEFINED_SUCCESS)
    assert findings and "no named evidence" in findings[0].detail


def test_a_model_judge_in_a_deterministic_verdict_path_is_redefined_success():
    report = scan(
        test_claims=[
            EvaluationClaim(gate_id="GATE-D1-03", evidence=("x",), model_judge_in_verdict_path=True)
        ]
    )
    assert any("model judge" in f.detail for f in report.of_type(DriftFinding.REDEFINED_SUCCESS))


def test_outside_allowed_paths_is_detected():
    report = scan(active_tasks=[clean_task(changed_paths=("/etc/passwd",), allowed_paths=("src/**",))])
    assert report.of_type(DriftFinding.OUTSIDE_ALLOWED_PATHS)


def test_writing_a_pinned_gate_file_is_outside_allowed_paths():
    report = scan(
        active_tasks=[
            clean_task(changed_paths=("project-pack/acceptance/visible/GATE-D1-03-x.yaml",), allowed_paths=("**",))
        ]
    )
    assert report.of_type(DriftFinding.OUTSIDE_ALLOWED_PATHS)


def test_stale_contract_version_is_detected_on_a_task_and_on_an_artifact():
    report = scan(
        active_tasks=[clean_task(contract_version="1.0")],
        artifacts=[ArtifactClaim(artifact_id="ART-OLD", contract_version="1.0", produced_by_task="TSK-001")],
    )
    subjects = {f.subject for f in report.of_type(DriftFinding.STALE_CONTRACT_VERSION)}
    assert {"TSK-001", "ART-OLD"} <= subjects


def test_stale_input_artifact_is_detected():
    report = scan(
        active_tasks=[clean_task(input_hashes={"project-pack": "sha256:old"})],
        current_input_hashes={"project-pack": "sha256:new"},
    )
    assert report.of_type(DriftFinding.STALE_INPUT_ARTIFACT)


def test_duplicate_or_conflicting_work_is_detected():
    report = scan(
        active_tasks=[
            clean_task(task_id="TSK-001", changed_paths=("src/contracts/compiler.py",)),
            clean_task(
                task_id="TSK-002",
                requirement_ids=tuple(compiled().tasks["TSK-002"]["requirement_ids"]),
                changed_paths=("src/contracts/compiler.py",),
            ),
        ]
    )
    assert report.of_type(DriftFinding.DUPLICATE_OR_CONFLICTING_WORK)


def test_role_conflict_is_detected_when_one_alias_holds_two_incompatible_roles():
    report = scan(
        active_tasks=[
            clean_task(role_assignments={"implementer": "implementer-i12", "judge": "implementer-i12"})
        ]
    )
    findings = report.of_type(DriftFinding.ROLE_CONFLICT)
    assert findings and "implementer-i12" in findings[0].detail


def test_role_conflict_does_not_fire_for_distinct_aliases():
    report = scan(
        active_tasks=[clean_task(role_assignments={"implementer": "implementer-i12", "judge": "judge-j03"})]
    )
    assert report.of_type(DriftFinding.ROLE_CONFLICT) == []


def test_protected_asset_access_is_detected():
    report = scan(
        active_tasks=[clean_task(changed_paths=("efah-lab-verifier/holdouts/secret.py",), allowed_paths=("**",))]
    )
    assert report.of_type(DriftFinding.PROTECTED_ASSET_ACCESS)


def test_missing_wiring_is_detected_when_a_task_claims_completion():
    report = scan(
        active_tasks=[clean_task(state=str(TaskState.CANDIDATE_COMPLETE), wiring_manifest={"provides": ["x"]})]
    )
    findings = report.of_type(DriftFinding.MISSING_WIRING)
    assert findings and "e2e_path" in findings[0].detail


def test_missing_wiring_does_not_fire_for_a_complete_manifest():
    manifest = {
        "provides": ["compiler"],
        "consumes": ["pack"],
        "startup_registration": True,
        "configuration_schema": "efah.compiler_config",
        "health_check": "/health/compiler",
        "integration_test": "tests/integration/test_compiler_path.py",
        "e2e_path": "harness project run",
        "telemetry_span": "efah.compile",
        "dashboard_projection": "project.compilation",
    }
    report = scan(active_tasks=[clean_task(state=str(TaskState.CANDIDATE_COMPLETE), wiring_manifest=manifest)])
    assert report.of_type(DriftFinding.MISSING_WIRING) == []


def test_unsupported_reimplementation_is_detected_without_a_build_vs_integrate_record():
    report = scan(active_tasks=[clean_task(introduces_components=("custom_workflow_engine",))])
    assert report.of_type(DriftFinding.UNSUPPORTED_REIMPLEMENTATION)


def test_unsupported_reimplementation_is_cleared_by_a_build_vs_integrate_record():
    report = scan(
        active_tasks=[
            clean_task(
                introduces_components=("custom_workflow_engine",),
                build_vs_integrate_record={"capability": "workflow", "rejected_reimplementation": False},
            )
        ]
    )
    assert report.of_type(DriftFinding.UNSUPPORTED_REIMPLEMENTATION) == []


def test_release_missing_a_blocking_gate_result_is_a_finding():
    report = scan(release=ReleaseContents(commit="deadbeef", task_ids=("TSK-001",), gate_results={}))
    assert report.of_type(DriftFinding.REQUIREMENT_WEAKENING)


def test_release_with_a_failing_blocking_gate_is_redefined_success():
    gate_results = {g: "PASS" for g, b in compiled().gates.items() if b.get("blocking")}
    gate_results["GATE-D1-10"] = "FAIL"
    report = scan(release=ReleaseContents(commit="deadbeef", gate_results=gate_results))
    assert any(f.subject == "GATE-D1-10" for f in report.of_type(DriftFinding.REDEFINED_SUCCESS))


def test_all_thirteen_section_19_2_finding_types_are_reachable():
    """Section 19.2's list is closed; every member must be producible."""
    produced: set[str] = set()
    gates = {k: dict(v) for k, v in compiled().gates.items()}
    gates["GATE-D1-10"]["blocking"] = False
    gates["GATE-D1-03"] = dict(gates["GATE-D1-03"])
    assertions = [dict(a) for a in gates["GATE-D1-03"]["assertions"]]
    assertions[0]["expected"] = "whatever"
    gates["GATE-D1-03"]["assertions"] = assertions

    report = scan(
        observed_gates=gates,
        active_tasks=[
            clean_task(
                task_id="TSK-SMUGGLED",
                requirement_ids=(),
                contract_version="1.0",
                state=str(TaskState.CANDIDATE_COMPLETE),
                changed_paths=("efah-lab-verifier/x.py", "/etc/passwd", "shared.py"),
                allowed_paths=("src/**",),
                role_assignments={"implementer": "a1", "judge": "a1"},
                introduces_components=("custom_graph_database",),
                input_hashes={"pack": "sha256:old"},
            ),
            clean_task(changed_paths=("shared.py",), allowed_paths=("**",)),
        ],
        current_input_hashes={"pack": "sha256:new"},
        security_findings=[
            security.SecurityFinding(
                finding_id="SEC-OOS",
                title="rewrite auth in rust",
                proposed_requirements=("REQ-NEW-001",),
            )
        ],
    )
    produced.update(report.types_found())

    expected = {
        str(DriftFinding.UNLINKED_TASK),
        str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
        str(DriftFinding.REQUIREMENT_WEAKENING),
        str(DriftFinding.REDEFINED_SUCCESS),
        str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
        str(DriftFinding.STALE_CONTRACT_VERSION),
        str(DriftFinding.STALE_INPUT_ARTIFACT),
        str(DriftFinding.DUPLICATE_OR_CONFLICTING_WORK),
        str(DriftFinding.ROLE_CONFLICT),
        str(DriftFinding.PROTECTED_ASSET_ACCESS),
        str(DriftFinding.MISSING_WIRING),
        str(DriftFinding.UNSUPPORTED_REIMPLEMENTATION),
        str(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION),
    }
    assert expected <= produced, sorted(expected - produced)


# --------------------------------------------------------------------------
# Section 19.5 — GATE-D2-21 A4 and A5


def approved_refs():
    return engine().approved_security_refs()


def in_scope_finding(**overrides):
    defaults = dict(
        finding_id="SEC-001",
        title="protected identity database reachable with the main admin credential",
        mapped_refs=("protected_verifier_isolation",),
        evidence=("curl transcript: 200 from :6363 admin against the protected canary",),
        executable_probe="pytest tests/integration/test_protected_isolation.py",
        smallest_remediation="bind the protected instance to 127.0.0.1 on its own port and rotate its admin password",
        severity="high",
    )
    defaults.update(overrides)
    return security.SecurityFinding(**defaults)


def test_a_fully_qualified_security_finding_blocks():
    classification = security.classify(in_scope_finding(), approved_refs())
    assert classification.blocks is True
    assert classification.finding_type is None
    assert classification.admitted_refs == ("protected_verifier_isolation",)
    assert classification.unsatisfied == ()


@pytest.mark.parametrize(
    "missing,condition",
    [
        ({"mapped_refs": ()}, security.CONDITION_MAPPED),
        ({"evidence": (), "executable_probe": None}, security.CONDITION_EVIDENCE),
        ({"smallest_remediation": None}, security.CONDITION_REMEDIATION),
    ],
)
def test_each_missing_section_19_5_condition_demotes_the_finding(missing, condition):
    classification = security.classify(in_scope_finding(**missing), approved_refs())
    assert classification.blocks is False
    assert classification.finding_type == str(DriftFinding.OUT_OF_SCOPE_OBSERVATION)
    assert condition in classification.unsatisfied


def test_a_finding_cannot_mint_its_own_authority():
    """A reference the approved set does not contain grants nothing."""
    classification = security.classify(
        in_scope_finding(mapped_refs=("REQ-I-JUST-MADE-THIS-UP",)), approved_refs()
    )
    assert classification.blocks is False
    assert "not in the approved set" in classification.rationale


def test_an_out_of_scope_security_finding_is_demonstrably_rejected():
    """GATE-D2-21 A4: classified as an observation and does not block."""
    finding = security.SecurityFinding(
        finding_id="SEC-OOS-1",
        title="add a WAF, rotate all credentials weekly, and adopt mTLS everywhere",
        severity="critical",
    )
    report = security.admit([finding], approved_refs())
    assert report.blocking == []
    assert len(report.observations) == 1
    assert report.observations[0].finding_type == str(DriftFinding.OUT_OF_SCOPE_OBSERVATION)
    assert report.expands_the_build is False

    drift_report = scan(security_findings=[finding])
    assert drift_report.of_type(DriftFinding.OUT_OF_SCOPE_OBSERVATION)
    assert drift_report.blocking == []
    assert drift_report.unresolved_scope_drift == 0


def test_an_out_of_scope_finding_that_proposes_work_is_an_expansion():
    finding = security.SecurityFinding(
        finding_id="SEC-OOS-2",
        title="adopt a formal threat-modelling programme",
        proposed_requirements=("REQ-NEW-900",),
        proposed_tasks=("TSK-NEW-900",),
    )
    report = security.admit([finding], approved_refs())
    assert report.expands_the_build is True
    assert report.expansions[0]["finding"] == str(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION)

    drift_report = scan(security_findings=[finding])
    assert drift_report.of_type(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION)
    assert drift_report.blocking


def test_blocking_finding_schema_assert_lists_what_is_missing():
    """GATE-D2-21 A5."""
    assert security.blocking_schema_violations(in_scope_finding()) == []
    missing = security.blocking_schema_violations(
        in_scope_finding(evidence=(), executable_probe=None, smallest_remediation="  ")
    )
    assert set(missing) == {security.CONDITION_EVIDENCE, security.CONDITION_REMEDIATION}


def test_detect_expansion_flags_requirements_added_while_observations_are_open():
    observations = [security.classify(in_scope_finding(mapped_refs=()), approved_refs())]
    assert security.detect_expansion(["REQ-1"], ["REQ-1"], observations) == []
    expansions = security.detect_expansion(["REQ-1"], ["REQ-1", "REQ-2"], observations)
    assert expansions and expansions[0]["added_requirement_ids"] == ["REQ-2"]


# --------------------------------------------------------------------------
# Sections 19.3 and 19.4 — GATE-D2-22


def test_scheduler_reads_the_interval_and_events_from_the_pack():
    scheduler = ContractReviewScheduler.from_pack(load_pack(PACK_ROOT))
    assert scheduler.interval_material_phases == 3
    assert "after_walking_skeleton" in scheduler.event_triggers
    assert len(scheduler.event_triggers) == 13


def test_interval_defaults_to_three_material_phases_when_omitted():
    scheduler = ContractReviewScheduler(None, [])
    assert scheduler.interval_material_phases == DEFAULT_INTERVAL_MATERIAL_PHASES == 3


def test_review_fires_after_the_configured_number_of_material_phases():
    scheduler = ContractReviewScheduler(3, [])
    assert scheduler.observe_phase() is None
    assert scheduler.observe_phase() is None
    trigger = scheduler.observe_phase()
    assert trigger is not None and trigger.trigger_type == "periodic"
    # counter resets, so the next review is another three phases away
    assert scheduler.observe_phase() is None


def test_non_material_phases_do_not_advance_the_counter():
    scheduler = ContractReviewScheduler(2, [])
    assert scheduler.observe_phase(material=False) is None
    assert scheduler.observe_phase(material=False) is None
    assert scheduler.observe_phase() is None
    assert scheduler.observe_phase() is not None


def test_review_fires_at_an_event_trigger():
    scheduler = ContractReviewScheduler.from_pack(load_pack(PACK_ROOT))
    trigger = scheduler.due_for_event("after_walking_skeleton")
    assert trigger is not None and trigger.trigger_type == "event"
    assert scheduler.due_for_event("because_i_felt_like_it") is None


def test_only_contract_reaffirmed_advances_automatically():
    for outcome in ContractReviewOutcome:
        advances = ContractReviewScheduler.advances_automatically(outcome)
        assert advances is (outcome is ContractReviewOutcome.CONTRACT_REAFFIRMED)


def test_a_clean_review_reaffirms_and_advances():
    scheduler = ContractReviewScheduler.from_pack(load_pack(PACK_ROOT))
    review = scheduler.review(
        review_id="CR-001",
        trigger=ReviewTrigger("CRT-EV-02", "event", "after walking skeleton", event="after_walking_skeleton"),
        drift_findings=[],
        requirements_before=["REQ-AC-001"],
        requirements_after=["REQ-AC-001"],
        evidence=["pytest -q"],
    )
    assert review.outcome is ContractReviewOutcome.CONTRACT_REAFFIRMED
    assert review.advances_automatically is True
    assert review.scope_expanded is False


def test_injected_drift_halts_automatic_advance_and_names_a_remediation_route():
    scheduler = ContractReviewScheduler(3, [])
    review = scheduler.review(
        review_id="CR-002",
        trigger=ReviewTrigger("CRT-INTERVAL", "periodic", "3 phases", phases_since_last_review=3),
        drift_findings=[{"finding": str(DriftFinding.UNLINKED_TASK), "subject": "TSK-X"}],
        requirements_before=["REQ-AC-001"],
        requirements_after=["REQ-AC-001"],
    )
    assert review.outcome is ContractReviewOutcome.DRIFT_DETECTED
    assert review.advances_automatically is False
    assert review.as_body()["remediation_route"] == "scope_drift_remediation"


def test_a_review_that_adds_requirements_is_itself_drift():
    """Section 19.4: review is conformance checking, not an improvement round."""
    scheduler = ContractReviewScheduler(3, [])
    review = scheduler.review(
        review_id="CR-003",
        trigger=ReviewTrigger("CRT-INTERVAL", "periodic", "3 phases"),
        drift_findings=[],
        requirements_before=["REQ-AC-001"],
        requirements_after=["REQ-AC-001", "REQ-NICE-TO-HAVE"],
    )
    assert review.outcome is ContractReviewOutcome.DRIFT_DETECTED
    assert review.scope_expanded is True
    assert review.as_body()["scope_expansion_finding"] == str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION)


def test_amendment_required_routes_to_a_typed_owner_blocker():
    scheduler = ContractReviewScheduler(3, [])
    review = scheduler.review(
        review_id="CR-004",
        trigger=ReviewTrigger("CRT-EV-01", "event", "before implementation"),
        drift_findings=[],
        requirements_before=[],
        requirements_after=[],
        amendment_required=True,
    )
    assert review.outcome is ContractReviewOutcome.AMENDMENT_REQUIRED
    assert review.as_body()["terminal_state"] == str(ProjectState.BLOCKED_OWNER_DECISION)


def test_scheduler_refuses_a_nonsensical_interval():
    with pytest.raises(ValueError):
        ContractReviewScheduler(0, [])
