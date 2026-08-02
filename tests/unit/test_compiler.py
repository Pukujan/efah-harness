"""Contract compiler unit tests.

Covers the Section 8 envelope invariants, the Section 13.3 applicability
compiler, the Section 1.3 steps 6 and 7 recompilation for AMENDMENT-001, DEC-005,
and the Section 6 CLI. Every negative control here fails if the corresponding
guard is removed.
"""

from __future__ import annotations

import functools
import shutil
from pathlib import Path

import pytest
import yaml

from cli.main import EXIT_CODES, build_parser, main, run_project
from contracts import markdown
from contracts.compiler import ContractCompiler, compile_pack
from contracts.plan import PLAN_ITEMS, validate_against_pack
from governance.compiler import COMPILER_ALIAS, CompilationError, emit, verify
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION, CompiledObject
from governance.states import ContractReviewOutcome, ProjectState
from impact import revalidation
from integrations.pack import load_pack
from methodologies.applicability import ApplicabilityCompiler, MethodologyPolicyError
from requirements.catalog import build_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "project-pack"


@functools.lru_cache(maxsize=1)
def compiled():
    return compile_pack(load_pack(PACK_ROOT), repo_root=REPO_ROOT)


# --------------------------------------------------------------------------
# Section 8 envelope invariants


def test_every_compiled_object_carries_a_sealed_v1_1_envelope():
    objects = compiled().all_objects
    assert len(objects) > 1000
    for obj in objects:
        envelope = obj.envelope
        assert envelope.contract_id == CONTRACT_ID
        assert envelope.contract_version == "1.1"
        assert envelope.schema_id
        assert envelope.content_hash and envelope.content_hash.startswith("sha256:")
        assert envelope.created_by_alias == COMPILER_ALIAS
        assert obj.is_intact()


def test_content_hash_binds_the_body():
    obj = emit("efah.test", {"value": 1})
    assert obj.is_intact()
    tampered = CompiledObject(envelope=obj.envelope, body={"value": 2})
    assert not tampered.is_intact()
    with pytest.raises(CompilationError):
        verify(tampered)


def test_emitter_rejects_a_wrong_contract_version():
    obj = emit("efah.test", {"value": 1})
    stale = CompiledObject(
        envelope=obj.envelope.model_copy(update={"contract_version": "1.0"}),
        body=obj.body,
    )
    with pytest.raises(CompilationError, match="contract_version"):
        verify(stale)


def test_emitter_rejects_a_missing_schema_id():
    with pytest.raises(CompilationError):
        emit("", {"value": 1})


def test_identical_bodies_hash_identically_and_different_bodies_do_not():
    left = emit("efah.test", {"b": 2, "a": 1})
    right = emit("efah.test", {"a": 1, "b": 2})
    other = emit("efah.test", {"a": 1, "b": 3})
    # created_at differs, so compare the body hash the envelope binds
    assert left.envelope.sealed(left.body).content_hash == left.envelope.sealed(right.body).content_hash
    assert left.envelope.sealed(left.body).content_hash != left.envelope.sealed(other.body).content_hash


# --------------------------------------------------------------------------
# requirement catalog


def test_catalog_compiles_every_requirement_family_from_the_pack():
    catalog = build_catalog(load_pack(PACK_ROOT))
    kinds = {r.kind for r in catalog.requirements}
    assert kinds == {"acceptance_check", "phase", "compiler_output", "auto_merge", "non_goal", "drift_finding"}
    assert len(catalog.ids_of_kind("acceptance_check")) == 27
    assert catalog.findings == []
    for requirement in catalog.requirements:
        assert requirement.source, requirement.requirement_id


def test_amendment_added_checks_compile_to_gate_linked_requirements():
    catalog = build_catalog(load_pack(PACK_ROOT))
    for check, gate_id in (
        ("owner_control_surface_vendor_neutral", "GATE-D1-10"),
        ("vendor_neutral_after_deadline", "GATE-D1-07"),
    ):
        requirement = catalog.get(catalog.by_check[check])
        assert requirement.gate_ids == (gate_id,)
        assert requirement.blocking is True


def test_negative_control_acceptance_check_without_a_gate_is_a_finding(tmp_path: Path):
    """Remove one gate mapping from the index; the check must become a finding."""
    pack_copy = tmp_path / "project-pack"
    shutil.copytree(PACK_ROOT, pack_copy)
    index_path = pack_copy / "acceptance" / "visible" / "INDEX.yaml"
    index = yaml.safe_load(index_path.read_text())
    index["coverage"] = [row for row in index["coverage"] if row["check"] != "owner_control_surface_vendor_neutral"]
    index_path.write_text(yaml.safe_dump(index))

    catalog = build_catalog(load_pack(pack_copy))
    kinds = {(f.kind, f.subject) for f in catalog.findings}
    assert ("ACCEPTANCE_CHECK_WITHOUT_GATE", "owner_control_surface_vendor_neutral") in kinds
    assert ("GATE_WITHOUT_ACCEPTANCE_CHECK", "GATE-D1-10") in kinds
    assert all(f.failure_state is ProjectState.FAILED_CONTRACT for f in catalog.findings)


# --------------------------------------------------------------------------
# Section 13.3 applicability compiler


def test_applicability_is_deterministic_and_catalog_bound():
    compiler = ApplicabilityCompiler(load_pack(PACK_ROOT))
    first = compiler.select(task_id="T", task_class="trust_critical_code_change", risk="high")
    second = compiler.select(task_id="T", task_class="trust_critical_code_change", risk="high")
    assert first == second
    assert first.methodology_source == "applicability_compiler"
    assert set(first.required) == {"M-02", "M-05", "M-06", "M-09", "M-12", "M-21"}
    assert "external_research" in first.conditional


def test_applicability_refuses_an_unknown_task_class():
    compiler = ApplicabilityCompiler(load_pack(PACK_ROOT))
    with pytest.raises(MethodologyPolicyError, match="no applicability rule"):
        compiler.select(task_id="T", task_class="whatever_feels_relevant", risk="high")


def test_risk_any_rule_matches_every_risk():
    compiler = ApplicabilityCompiler(load_pack(PACK_ROOT))
    low = compiler.select(task_id="T", task_class="research_or_debugging", risk="low")
    high = compiler.select(task_id="T", task_class="research_or_debugging", risk="high")
    assert low.required == high.required


# --------------------------------------------------------------------------
# plan table vs the contract's own three-day plan


def test_plan_table_matches_the_contracts_three_day_plan():
    validation = validate_against_pack(load_pack(PACK_ROOT))
    assert validation.missing_from_table == {}
    assert validation.extra_in_table == {}
    assert validation.unknown_checks == []


def test_exactly_one_plan_item_is_amendment_added():
    added = [item for item in PLAN_ITEMS if item.amendment_added]
    assert [item.key for item in added] == ["owner_control_surface"]
    assert added[0].day == 1


# --------------------------------------------------------------------------
# Section 1.3 steps 6 and 7 — AMENDMENT-001


def test_step_6_gate_d1_10_is_in_the_recompiled_gate_set_blocking_and_day_1():
    step_6 = compiled().recompilation.step_6_body()
    assert "GATE-D1-10" in step_6["gate_ids"]
    assert step_6["gates_added_by_amendment"] == ["GATE-D1-10"]
    assert "GATE-D1-10" in step_6["blocking_gate_ids"]
    assert "GATE-D1-10" in step_6["day_1_gate_ids"]
    assert step_6["amendment_gate_is_blocking_day_1"] is True
    assert step_6["gate_count"] == 27


def test_step_6_walking_skeleton_gains_owner_control_surface_after_dashboard_update():
    skeleton = compiled().recompilation.walking_skeleton
    assert skeleton.added_step == "owner control surface"
    assert skeleton.inserted_after == "dashboard update"
    assert len(skeleton.after) == len(skeleton.before) + 1
    index = skeleton.after.index("owner control surface")
    assert skeleton.after[index - 1] == "dashboard update"
    assert skeleton.before[0] == "project-pack import"


def test_step_7_emits_exactly_four_revalidation_records():
    records = compiled().recompilation.revalidation_records
    assert [r.object_ref for r in records] == ["§11.3", "§11.6", "§14.4", "§10.7"]
    changed = {r.object_ref: r.changed for r in records}
    assert changed == {"§11.3": True, "§11.6": True, "§14.4": True, "§10.7": False}
    for record in records:
        assert record.reason, record.object_ref
        assert record.to_contract_version == "1.1"
        assert record.outcome == str(ContractReviewOutcome.CONTRACT_REAFFIRMED)
    unchanged = next(r for r in records if r.object_ref == "§10.7")
    assert unchanged.revalidation_action == "reaffirm_without_change"
    assert "does not create new interrupt types" in unchanged.reason


def test_step_7_records_are_emitted_as_compiled_objects():
    project = compiled()
    records = [
        obj for obj in project.outputs["requirements"] if obj.envelope.schema_id == "efah.revalidation_record"
    ]
    assert len(records) == 4
    assert all(obj.envelope.contract_version == "1.1" for obj in records)


def test_amendment_refuses_a_walking_skeleton_anchor_it_cannot_find():
    contract_md = (PACK_ROOT / "contract.md").read_text().replace("dashboard update", "dashboard refresh")
    amendment = (PACK_ROOT / "evidence" / "owner-documents" / revalidation.AMENDMENT_FILENAME).read_text()
    with pytest.raises(revalidation.AmendmentError, match="not a Section 14.4 step"):
        revalidation.recompile_walking_skeleton(contract_md, amendment)


def test_amendment_refuses_a_gate_set_missing_gate_d1_10():
    pack = load_pack(PACK_ROOT)
    gates = {k: v for k, v in pack.acceptance_gates().items() if k != "GATE-D1-10"}
    with pytest.raises(revalidation.AmendmentError, match="absent from the recompiled gate set"):
        revalidation.recompile(
            pack_root=pack.root,
            contract_md=pack.files["contract.md"].parsed,
            gates=gates,
            project_yaml_text=(PACK_ROOT / "project.yaml").read_text(),
            decisions_dir=REPO_ROOT / "docs" / "decisions",
        )


def test_amendment_refuses_a_non_blocking_gate_d1_10():
    pack = load_pack(PACK_ROOT)
    gates = {k: dict(v) for k, v in pack.acceptance_gates().items()}
    gates["GATE-D1-10"]["blocking"] = False
    with pytest.raises(revalidation.AmendmentError, match="not blocking"):
        revalidation.recompile(
            pack_root=pack.root,
            contract_md=pack.files["contract.md"].parsed,
            gates=gates,
            project_yaml_text=(PACK_ROOT / "project.yaml").read_text(),
            decisions_dir=REPO_ROOT / "docs" / "decisions",
        )


# --------------------------------------------------------------------------
# DEC-005


def test_dec_005_puts_gate_d1_10_before_gate_d1_07_without_weakening_either():
    priority = compiled().recompilation.delivery_priority
    assert priority.gate_order == ["GATE-D1-10", "GATE-D1-07"]
    assert priority.superseded_gate_order == ["GATE-D1-07", "GATE-D1-10"]
    assert priority.gates_still_blocking_all()


def test_dec_005_is_recorded_as_a_priority_decision_not_an_amendment():
    body = compiled().recompilation.delivery_priority.as_body()
    assert body["interrupt_class"] == "OWNER_PRIORITY_DECISION"
    assert body["reorder_not_weakening"] is True
    assert body["supersedes"] == "project-pack/project.yaml#delivery_priority"


def test_dec_005_must_exist_on_disk():
    with pytest.raises(revalidation.AmendmentError, match="DEC-005 not found"):
        revalidation.compile_delivery_priority(REPO_ROOT / "docs" / "no-such-dir", "delivery_priority:\n  - a\n")


def test_superseded_gate_order_is_read_from_the_raw_project_yaml_comments():
    order = revalidation.superseded_gate_order((PACK_ROOT / "project.yaml").read_text())
    assert order == ["GATE-D1-07", "GATE-D1-10"]


# --------------------------------------------------------------------------
# markdown structure reads


def test_markdown_reads_the_section_27_evidence_package():
    contract_md = (PACK_ROOT / "contract.md").read_text()
    fields = markdown.fenced_block(markdown.section(contract_md, "27. Final Evidence Package"))
    assert "Project status: VERIFIED_COMPLETE" in fields
    assert len(fields) >= 20


def test_markdown_reads_section_5_layout_with_indentation_preserved():
    contract_md = (PACK_ROOT / "contract.md").read_text()
    layout = markdown.fenced_block(markdown.section(contract_md, "5. Repository and Modular-Monolith"))
    assert "src/" in layout
    assert any(line.startswith("  ") for line in layout)


def test_compiled_path_policy_lists_the_section_5_modules_and_the_sealed_repos():
    policy = next(
        obj.body
        for obj in compiled().outputs["allowed_and_prohibited_paths"]
        if obj.body.get("scope") == "project"
    )
    assert "contracts" in policy["declared_modules"]
    assert "drift" in policy["declared_modules"]
    assert "unit" not in policy["declared_modules"]  # tests/ subdirs are not src modules
    assert "efah-lab-verifier" in policy["sealed_repository_names"]
    assert "project-pack/acceptance/visible/**" in policy["prohibited_paths"]


# --------------------------------------------------------------------------
# compiler-level guards


def test_compiler_refuses_a_pack_bound_to_a_stale_contract_version(tmp_path: Path):
    pack_copy = tmp_path / "project-pack"
    shutil.copytree(PACK_ROOT, pack_copy)
    contract = yaml.safe_load((pack_copy / "contract.yaml").read_text())
    contract["contract"]["version"] = "1.0"
    (pack_copy / "contract.yaml").write_text(yaml.safe_dump(contract))
    with pytest.raises(CompilationError, match="STALE_CONTRACT_VERSION"):
        compile_pack(load_pack(pack_copy), repo_root=REPO_ROOT)


def test_compiler_refuses_an_output_name_the_contract_does_not_declare():
    compiler = ContractCompiler(load_pack(PACK_ROOT), repo_root=REPO_ROOT)
    project = compiler.compile()
    with pytest.raises(CompilationError, match="compiler outputs"):
        compiler._emit(project, "helpful_extra_output", "efah.test", {})


def test_role_separation_is_cross_family_everywhere_it_must_be():
    edges = [
        obj.body
        for obj in compiled().outputs["role_separation"]
        if obj.envelope.schema_id == "efah.role_validation_edge"
    ]
    assert edges
    same_family = [e for e in edges if not e["cross_family"]]
    assert same_family == [], same_family


def test_mandatory_role_incompatibilities_are_satisfied_by_the_alias_map():
    rules = [
        obj.body
        for obj in compiled().outputs["role_separation"]
        if obj.envelope.schema_id == "efah.role_incompatibility"
    ]
    assert len(rules) == 7
    for rule in rules:
        if rule["mandatory"]:
            assert rule["satisfied"], rule


def test_gate_bearing_roles_route_to_the_eval_gateway():
    """DEC-002: routing a gate-bearing role to production is FAILED_PROVENANCE."""
    requirements = [
        obj.body
        for obj in compiled().outputs["model_capability_requirements"]
        if obj.envelope.schema_id == "efah.model_capability_requirement"
    ]
    assert len(requirements) == 15
    for requirement in requirements:
        if requirement["zero_retry_required"]:
            assert requirement["gateway_class"] == "eval", requirement["role"]
            assert requirement["sdk_max_retries"] == 0
        else:
            assert requirement["gateway_class"] == "production", requirement["role"]


def test_judge_is_advisory_until_calibrated():
    judge = next(
        obj.body
        for obj in compiled().outputs["model_capability_requirements"]
        if obj.envelope.schema_id == "efah.model_capability_requirement" and obj.body["role"] == "judge"
    )
    assert judge["advisory_only"] is True
    assert judge["calibration_required_before_gate_authority"] is True


# --------------------------------------------------------------------------
# Section 6 CLI


def test_cli_run_validates_compiles_and_reports():
    report = run_project(PACK_ROOT, mode="autonomous")
    assert report.validated is True
    assert report.compiled is True
    assert report.state is ProjectState.RUNNING
    assert report.compiler_summary["compiles"] is True
    assert report.compiler_summary["contract_version"] == CONTRACT_VERSION


def test_cli_reports_unmerged_lanes_by_name_instead_of_crashing():
    report = run_project(PACK_ROOT, mode="autonomous")
    lanes = {lane.name: lane for lane in report.lanes}
    assert set(lanes) == {"terminusdb_import", "langgraph_run"}
    for lane in lanes.values():
        if not lane.available:
            assert "WS-" in lane.detail, "an unavailable lane must name its owner"


def test_cli_reports_a_bad_pack_as_failed_contract(tmp_path: Path):
    report = run_project(tmp_path / "not-a-pack")
    assert report.validated is False
    assert report.state is ProjectState.FAILED_CONTRACT
    assert report.problems


def test_cli_parses_the_contract_command_line():
    args = build_parser().parse_args(["project", "run", "./project-pack", "--mode", "autonomous"])
    assert (args.domain, args.action, args.pack, args.mode) == ("project", "run", "./project-pack", "autonomous")


def test_cli_main_exits_zero_on_the_real_pack(capsys):
    code = main(["project", "compile", str(PACK_ROOT), "--json"])
    assert code == EXIT_CODES[ProjectState.RUNNING] == 0
    assert "compiler" in capsys.readouterr().out


def test_cli_writes_the_gate_evidence_bundle(tmp_path: Path, capsys):
    out = tmp_path / "evidence"
    main(["project", "compile", str(PACK_ROOT), "--out", str(out)])
    capsys.readouterr()
    written = sorted(p.name for p in out.glob("*.json"))
    assert "compiler-output-manifest.json" in written
    assert "critical-path-listing.json" in written
    assert "cycle-detection-report.json" in written
    assert "graph-export-with-edge-types.json" in written
    assert "amendment-001-recompilation.json" in written
