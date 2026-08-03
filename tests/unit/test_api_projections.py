"""Read projections (contract Sections 5.1, 9.8, 11.2, 11.6, 17.2).

The two properties this module exists to prove:

* the dashboard **cannot** mutate authoritative state -- by construction, not by
  convention (Section 5.1);
* a projection **cannot** leak protected content or a real model identity
  (Section 11.2, Section 17.2, ``plane.yaml -> protected_content_rule``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.adapters.control_plane_memory import InMemoryControlPlane
from api.state import (
    ControlPlaneSnapshot,
    DependencyRecord,
    DriftFindingRecord,
    EvaluationRecord,
    KnowledgeRecord,
    LeaseRecord,
    ModelRunRecord,
    OracleHealthRecord,
    ProjectRecord,
    ProvenanceEdge,
    ReleaseRecord,
    RequirementRecord,
    TaskRecord,
    TimingRecord,
)
from dashboard.projections import build_projection, derived_durations, project_from_source
from dashboard.redaction import ProtectedContentLeak, assert_no_protected_content
from dashboard.source import MutationAttemptedFromDashboard, ReadOnlySource
from dashboard.views import REQUIRED_VIEWS
from governance.envelope import KnowledgeTier
from governance.protected import sealed_repository_names
from governance.states import DriftFinding, OwnerInterrupt, ProjectState, TaskState, Verdict
from observability.identity import ProtectedIdentityLeak

DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "dashboard"


def snapshot(**overrides) -> ControlPlaneSnapshot:
    base = dict(
        project=ProjectRecord(
            project_id="EFAH-001",
            name="EFAH",
            state=ProjectState.RUNNING,
            contract_id="EFAH-CONTRACT-001",
            contract_version="1.1",
        ),
        tasks=(
            TaskRecord(
                task_id="T-1",
                project_id="EFAH-001",
                title="one",
                state=TaskState.RUNNING,
                requirement_ids=("R-1",),
                lease=LeaseRecord(
                    lease_id="L-1",
                    task_id="T-1",
                    holder_alias="implementer-i12",
                    worktree="wt/a",
                    fence_token=2,
                ),
                timing=TimingRecord(
                    queued_at="2026-08-02T05:00:00+00:00",
                    claimed_at="2026-08-02T05:00:30+00:00",
                    started_at="2026-08-02T05:01:00+00:00",
                    completed_at="2026-08-02T05:11:00+00:00",
                ),
            ),
            TaskRecord(
                task_id="T-2",
                project_id="EFAH-001",
                title="two",
                state=TaskState.BLOCKED_OWNER_DECISION,
                depends_on=("T-1",),
                typed_blocker="needs a scope call",
                owner_interrupt=OwnerInterrupt.OWNER_SCOPE_DECISION,
            ),
        ),
        requirements=(
            RequirementRecord(
                requirement_id="R-1",
                statement="the thing works",
                contract_section="11.6",
                verified_by_gate_ids=("GATE-D2-11",),
            ),
            RequirementRecord(requirement_id="R-2", statement="untraced", contract_section="11.6"),
        ),
        model_runs=(
            ModelRunRecord(
                run_id="MR-1",
                model_alias="implementer-i12",
                role="implementation_worker",
                gateway_class="production",
            ),
            ModelRunRecord(
                run_id="MR-2", model_alias="judge-j03", role="judge", gateway_class="production"
            ),
        ),
        evaluations=(
            EvaluationRecord(
                evaluation_id="EV-1",
                visible_verdict=Verdict.PASS,
                visible_passed=8,
                visible_total=10,
                hidden_suite_verdict=Verdict.FAIL,
                hidden_suite_name="holdout-a",
                hidden_assertions_total=4,
                hidden_assertions_failed=1,
            ),
        ),
        oracles=(
            OracleHealthRecord(
                oracle_id="ORACLE-001",
                oracle_version="1.0",
                healthy=True,
                mutants_total=10,
                mutants_killed=9,
                mutants_survived=1,
            ),
            OracleHealthRecord(
                oracle_id="ORACLE-002",
                oracle_version="1.0",
                healthy=False,
                model_judge_in_verdict_path=True,
            ),
        ),
        dependencies=(
            DependencyRecord(dependency_id="fastapi", component="fastapi", version="0.141.1"),
            DependencyRecord(
                dependency_id="langgraph", component="langgraph", version="TODO_builder_probe"
            ),
        ),
        knowledge=(
            KnowledgeRecord(
                knowledge_id="K-1", statement="verified", tier=KnowledgeTier.T7_HARD_GOLD,
                promoted_to_hard_gold=True,
            ),
            KnowledgeRecord(knowledge_id="K-2", statement="guess", tier=KnowledgeTier.T2_HYPOTHESIS),
        ),
        provenance=(
            ProvenanceEdge(source="a", relation="produced", target="b", terminus_commit="c1"),
            ProvenanceEdge(source="b", relation="produced", target="c"),
        ),
        drift_findings=(
            DriftFindingRecord(
                finding_id="D-1", finding_type=DriftFinding.UNLINKED_TASK, detail="no requirement"
            ),
        ),
        release=ReleaseRecord(
            release_id="RC-1", gates_required=("G1", "G2"), gates_passed=("G1",), ready=True
        ),
    )
    base.update(overrides)
    return ControlPlaneSnapshot(**base)


# ------------------------------------------------ read-only by construction


def test_read_only_source_exposes_no_write_method() -> None:
    control_plane = InMemoryControlPlane()
    source = ReadOnlySource(control_plane)
    for forbidden in ("import_project", "upsert_task", "record_decision", "set_project_state"):
        with pytest.raises(MutationAttemptedFromDashboard):
            getattr(source, forbidden)


def test_read_only_source_cannot_be_given_new_attributes() -> None:
    source = ReadOnlySource(InMemoryControlPlane())
    with pytest.raises(MutationAttemptedFromDashboard):
        source.import_project = lambda **_: None


def test_projection_refuses_a_writable_control_plane() -> None:
    """Accepting a bare control plane "for convenience" is how 5.1 gets broken."""
    with pytest.raises(TypeError):
        project_from_source(InMemoryControlPlane(), "EFAH-001")  # type: ignore[arg-type]


def test_no_module_in_the_dashboard_package_imports_a_write_path() -> None:
    """Architecture check: the dashboard has no route to a mutation."""
    forbidden_imports = {"httpx", "requests", "sqlite3", "terminusdb_client"}
    forbidden_names = {"ControlPlaneWritePort", "InMemoryControlPlane"}
    offenders: list[str] = []
    for path in DASHBOARD.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_imports:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in forbidden_imports:
                    offenders.append(f"{path.name}: from {node.module}")
                for alias in node.names:
                    if alias.name in forbidden_names:
                        offenders.append(f"{path.name}: imports {alias.name}")
    assert not offenders, offenders


def test_every_view_model_is_frozen() -> None:
    projection = build_projection(snapshot())
    with pytest.raises(ValidationError):
        projection.project_and_milestone_status.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------- the thirteen views


def test_all_thirteen_views_are_present_and_populated() -> None:
    """GATE-D2-11 A1."""
    projection = build_projection(snapshot())
    assert len(REQUIRED_VIEWS) == 13
    for name in REQUIRED_VIEWS:
        assert projection.view(name) is not None


def test_required_views_match_plane_yaml() -> None:
    import yaml

    pack_views = yaml.safe_load(Path("project-pack/plane.yaml").read_text())["required_views"]
    assert list(REQUIRED_VIEWS) == list(pack_views)


def test_view_contents_are_derived_not_invented() -> None:
    projection = build_projection(snapshot(), critical_path=("T-1", "T-2"))

    status = projection.project_and_milestone_status
    assert status.task_state_counts == {"RUNNING": 1, "BLOCKED_OWNER_DECISION": 1}
    assert status.is_terminal is False

    ledger = projection.task_ledger_and_critical_path
    assert ledger.critical_path == ("T-1", "T-2")
    assert ledger.unlinked_task_ids == ("T-2",)
    assert ledger.rows[0].total_wall_clock_seconds == 660.0

    ownership = projection.task_ownership_leases_worktrees_and_stale_sessions
    assert ownership.rows[0].holder_alias == "implementer-i12"
    assert ownership.rows[0].worktree == "wt/a"

    trace = projection.contract_and_requirement_traceability
    assert trace.untraced_requirement_ids == ("R-2",)
    assert trace.coverage_ratio == 0.5

    assert projection.scope_drift_findings.open_count == 1

    runs = projection.model_run_aliases_and_role_history
    assert runs.aliases_seen == ("implementer-i12", "judge-j03")
    # DEC-002: a judge on the production gateway is a provenance failure.
    assert runs.gate_bearing_runs_on_production_gateway == ("MR-2",)

    evaluation = projection.visible_and_hidden_evaluation_status
    assert evaluation.visible_pass_rate == 0.8
    assert evaluation.hidden_pass_rate == 0.75
    assert evaluation.protected_content_exposed is False

    oracles = projection.oracle_health_and_mutant_results
    assert oracles.unhealthy_oracle_ids == ("ORACLE-002",)
    assert oracles.oracles_with_judge_in_verdict_path == ("ORACLE-002",)
    assert oracles.surviving_mutants_total == 1

    dependencies = projection.dependency_versions_and_impact_maps
    assert dependencies.unpinned_dependency_ids == ("langgraph",)

    knowledge = projection.knowledge_and_hard_gold_promotion_state
    assert knowledge.hard_gold_count == 1
    assert knowledge.below_trust_floor_ids == ("K-2",)

    assert projection.provenance_graph.edges_without_commit_binding == 1

    release = projection.release_readiness
    assert release.ready is False
    assert release.gates_outstanding == ("G2",)

    blockers = projection.exact_typed_blocker_and_requested_owner_decision
    assert blockers.awaiting_owner is True
    assert blockers.requested_owner_decision.subject == "T-2"
    assert blockers.requested_owner_decision.owner_interrupt == OwnerInterrupt.OWNER_SCOPE_DECISION


# -------------------------------------------------------- Section 9.8 time


def test_durations_derive_from_system_events_only() -> None:
    timing = TimingRecord(
        queued_at="2026-08-02T05:00:00+00:00",
        claimed_at="2026-08-02T05:00:30+00:00",
        started_at="2026-08-02T05:01:00+00:00",
        blocked_at="2026-08-02T05:02:00+00:00",
        resumed_at="2026-08-02T05:04:00+00:00",
        completed_at="2026-08-02T05:11:00+00:00",
    )
    derived = derived_durations(timing)
    assert derived["queue_duration"] == 30.0
    assert derived["blocked_duration"] == 120.0
    assert derived["total_wall_clock"] == 660.0


def test_an_unmeasured_duration_is_none_not_zero() -> None:
    """Zero would read as "took no time"; None reads as "not measured"."""
    derived = derived_durations(TimingRecord())
    assert all(value is None for value in derived.values())


def test_timing_record_has_no_estimate_field() -> None:
    """Section 9.8: the schema is the enforcement, not a code review."""
    assert not any("estimate" in field for field in TimingRecord.model_fields)
    with pytest.raises(ValidationError):
        TimingRecord(estimated_hours=3)  # type: ignore[call-arg]


# ------------------------------------------------ protected content (11.2)


def test_a_projection_cannot_carry_a_real_model_id() -> None:
    """The test the brief asks for. Section 11.2: aliases only."""
    for real_identity in (
        "claude-opus-4-1-20250805",
        "gpt-4o",
        "gemini-2.5-pro",
        "anthropic/claude-sonnet-4",
        "meta-llama/Llama-3.1-70B",
    ):
        with pytest.raises(ProtectedIdentityLeak):
            ModelRunRecord(
                run_id="MR-X", model_alias=real_identity, role="judge", gateway_class="eval"
            )
        with pytest.raises(ProtectedIdentityLeak):
            assert_no_protected_content(
                {"rows": [{"run_id": "MR-X", "alias_display": real_identity}]}, where="test"
            )


def test_a_lease_holder_cannot_be_a_real_model_id() -> None:
    with pytest.raises(ProtectedIdentityLeak):
        LeaseRecord(lease_id="L", task_id="T", holder_alias="claude-3-5-sonnet")


def test_a_ranking_or_vendor_field_name_is_a_leak_by_itself() -> None:
    """Section 12.3 A5: no agent sees another's prestige or cost tier."""
    for field in ("vendor", "model_id", "cost_tier", "prestige_rank"):
        with pytest.raises(ProtectedIdentityLeak):
            assert_no_protected_content({"rows": [{field: "redacted"}]}, where="test")


def test_holdout_fixture_and_mutant_fields_are_refused() -> None:
    """plane.yaml -> protected_content_rule; GATE-D2-11 A3."""
    for field in (
        "holdout_assertions",
        "private_fixture",
        "mutant_source",
        "hidden_assertion_text",
    ):
        with pytest.raises(ProtectedContentLeak):
            assert_no_protected_content({field: "anything"}, where="test")


def test_assertion_and_key_material_is_refused() -> None:
    with pytest.raises(ProtectedContentLeak):
        assert_no_protected_content({"note": "assert result == 42"}, where="test")
    with pytest.raises(ProtectedContentLeak):
        assert_no_protected_content(
            {"note": "-----BEGIN RSA PRIVATE KEY-----"}, where="test"
        )


def test_a_route_to_the_sealed_repository_is_refused() -> None:
    with pytest.raises(ProtectedContentLeak):
        assert_no_protected_content(
            {"repo": f"github.com/x/{sealed_repository_names()[0]}"}, where="test"
        )


def test_the_evaluation_view_has_no_field_that_could_hold_content() -> None:
    """Structural: status is reportable because content is unrepresentable."""
    from dashboard.views import EvaluationStatusRow

    # Counts about protected artifacts are status and are allowed
    # (``hidden_assertions_failed: int``). A *string* field naming one would be
    # content, and there must be none.
    forbidden = {"assertion", "fixture", "mutant", "source", "body", "content", "diff", "holdout"}
    leaky = [
        name
        for name, field in EvaluationStatusRow.model_fields.items()
        if any(token in name for token in forbidden) and field.annotation not in (int, float)
    ]
    assert not leaky, leaky


def test_the_contracts_own_prose_is_still_renderable() -> None:
    """A scanner that blocks the contract text is a scanner nobody can ship.

    GATE-D1-07 A2's claim literally names the Anthropic SDK; the traceability
    view has to be able to show it.
    """
    assert_no_protected_content(
        {
            "rows": [
                {
                    "statement": "No essential module imports the Anthropic SDK "
                    "or a Claude-specific client."
                }
            ]
        },
        where="test",
    )


def test_a_real_projection_of_the_real_pack_passes_the_guard() -> None:
    control_plane = InMemoryControlPlane()
    control_plane.import_project(
        pack_root="project-pack", requested_by="test", correlation_id="c"
    )
    projection = project_from_source(ReadOnlySource(control_plane), "EFAH-001")
    assert projection is not None
    assert projection.contract_version == "1.1"
    assert projection.contract_and_requirement_traceability.rows
