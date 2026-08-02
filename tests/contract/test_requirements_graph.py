"""GATE-D1-03 — contract compiles to project/task/dependency graph.

Contract refs: Sections 8, 9.6. This file is the executable form of the gate's
five assertions, run against the real pack in ``project-pack/``. Each assertion
has a positive check on the real compilation and, where the assertion is about a
detector, a negative control proving the detector fires.

    A1 all seventeen contract_compiler_outputs are produced
    A2 every Task links to at least one Requirement
    A3 the dependency graph is acyclic where the contract requires it
    A4 a critical path is computed and non-empty
    A5 every task carries compiler-selected methodologies
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
import yaml

from contracts.compiler import SECTION_8_OUTPUTS, compile_pack
from governance.states import DriftFinding
from integrations.pack import load_pack
from methodologies.applicability import METHODOLOGY_SOURCE
from requirements.graph import DEPENDENCY_CLASSES, EDGE_TYPES, DependencyGraph, GraphError

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "project-pack"


@functools.lru_cache(maxsize=1)
def compiled():
    return compile_pack(load_pack(PACK_ROOT), repo_root=REPO_ROOT)


# --------------------------------------------------------------------------
# A1 — output manifest


def test_a1_all_seventeen_section_8_outputs_are_produced():
    manifest = compiled().manifest
    assert manifest is not None
    body = manifest.body
    assert body["section_8_output_count"] == 17
    assert body["all_seventeen_present"] is True
    missing = [name for name, info in body["section_8_outputs"].items() if not info["present"]]
    assert missing == [], missing


def test_a1_every_contract_yaml_output_key_has_objects():
    project = compiled()
    declared = project.manifest.body["contract_yaml_output_keys"]
    empty = [key for key in declared if not project.outputs.get(key)]
    assert empty == [], empty


def test_a1_section_8_bullets_map_onto_declared_keys():
    declared = set(compiled().manifest.body["contract_yaml_output_keys"])
    mapped = {key for _, keys in SECTION_8_OUTPUTS for key in keys}
    assert mapped == declared


# --------------------------------------------------------------------------
# A2 — zero unlinked tasks


def test_a2_zero_unlinked_tasks():
    assert compiled().graph.unlinked_tasks() == []


def test_a2_every_task_object_carries_requirement_ids():
    for task_id, task in compiled().tasks.items():
        assert task["requirement_ids"], f"{task_id} carries no requirement_ids"


def test_a2_negative_control_unlinked_task_is_detected():
    """The detector must fire, or A2 passing means nothing."""
    graph = DependencyGraph()
    graph.add_node("REQ-X", "Requirement")
    graph.add_node("TSK-LINKED", "Task")
    graph.add_node("TSK-ORPHAN", "Task")
    graph.add_edge("REQ-X", "TSK-LINKED", "implemented_by", dependency_class="requirement")
    assert graph.unlinked_tasks() == ["TSK-ORPHAN"]
    findings = graph.unlinked_task_findings()
    assert findings[0]["finding"] == str(DriftFinding.UNLINKED_TASK)


# --------------------------------------------------------------------------
# A3 — acyclic where required


def test_a3_zero_cycles_in_task_and_role_graphs():
    report = compiled().cycle_report
    assert report.cycles == [], report.cycles
    assert report.acyclic is True
    assert set(report.scanned) == {
        "task_depends_on",
        "task_blocks",
        "role_verified_by",
        "requirement_derived_from",
    }
    assert report.scanned["task_depends_on"] > 0
    assert report.scanned["role_verified_by"] > 0


def test_a3_negative_control_task_cycle_is_detected():
    graph = DependencyGraph()
    for name in ("A", "B", "C"):
        graph.add_node(name, "Task")
    graph.add_edge("A", "B", "depends_on", dependency_class="task")
    graph.add_edge("B", "C", "depends_on", dependency_class="task")
    graph.add_edge("C", "A", "depends_on", dependency_class="task")
    report = graph.cycles()
    assert not report.acyclic
    assert any(cycle[0] == "task_depends_on" for cycle in report.cycles)


def test_a3_negative_control_role_cycle_is_detected():
    """Circular validation: Section 12.2's builder-is-its-own-judge failure."""
    graph = DependencyGraph()
    graph.add_node("ROLE:implementer", "Role")
    graph.add_node("ROLE:judge", "Role")
    graph.add_edge("ROLE:implementer", "ROLE:judge", "verified_by", dependency_class="task")
    graph.add_edge("ROLE:judge", "ROLE:implementer", "verified_by", dependency_class="task")
    report = graph.cycles()
    assert any(cycle[0] == "role_verified_by" for cycle in report.cycles)


def test_a3_edge_vocabulary_is_closed():
    graph = DependencyGraph()
    graph.add_node("A", "Task")
    graph.add_node("B", "Task")
    with pytest.raises(GraphError):
        graph.add_edge("A", "B", "vaguely_related_to", dependency_class="task")
    with pytest.raises(GraphError):
        graph.add_edge("A", "MISSING", "depends_on", dependency_class="task")


# --------------------------------------------------------------------------
# A4 — critical path


def test_a4_critical_path_is_non_empty():
    path = compiled().critical_path
    assert path.length > 0
    assert path.nodes
    assert path.weight >= path.length


def test_a4_critical_path_is_a_real_chain_in_execution_order():
    project = compiled()
    depends_on = {
        (edge.source, edge.target)
        for edge in project.graph.edges_of_type("depends_on")
    }
    nodes = project.critical_path.nodes
    for prerequisite, dependent in zip(nodes, nodes[1:]):
        assert (dependent, prerequisite) in depends_on, f"{dependent} does not depend on {prerequisite}"


def test_a4_critical_path_starts_at_a_day_1_task_and_ends_at_a_day_3_task():
    project = compiled()
    nodes = project.critical_path.nodes
    days = [project.tasks[n]["day"] for n in nodes if n in project.tasks]
    assert days[0] == 1
    assert days[-1] == 3
    assert days == sorted(days), "critical path runs backwards through the plan"


def test_a4_negative_control_empty_graph_yields_no_path():
    assert DependencyGraph().critical_path().length == 0


# --------------------------------------------------------------------------
# A5 — methodology provenance


def test_a5_every_task_carries_compiler_selected_methodologies():
    for task_id, task in compiled().tasks.items():
        assert task["methodology_source"] == METHODOLOGY_SOURCE, task_id
        assert task["methodology_ids"], f"{task_id} selected no methodology"


def test_a5_methodology_ids_come_from_the_owner_catalog():
    project = compiled()
    catalog_objects = [
        obj for obj in project.outputs["methodologies_by_task_and_risk"]
        if obj.envelope.schema_id == "efah.methodology_catalog"
    ]
    assert len(catalog_objects) == 1
    known = {m["id"] for m in catalog_objects[0].body["methodologies"]}
    for task_id, task in project.tasks.items():
        unknown = set(task["methodology_ids"]) - known
        assert not unknown, f"{task_id} selected methods outside the catalog: {unknown}"


# --------------------------------------------------------------------------
# Section 9.6 dependency-map coverage


def test_all_fourteen_section_9_6_edge_types_are_emitted():
    counts = compiled().graph.edge_type_counts()
    assert set(counts) == set(EDGE_TYPES)
    unused = [edge_type for edge_type, count in counts.items() if count == 0]
    assert unused == [], f"Section 9.6 edge types with no edges: {unused}"


def test_dependency_map_covers_every_section_9_6_class():
    covered = {edge.dependency_class for edge in compiled().graph.edges}
    assert covered == set(DEPENDENCY_CLASSES), sorted(set(DEPENDENCY_CLASSES) - covered)


# --------------------------------------------------------------------------
# gate evidence and integrity


def test_gate_evidence_bundle_has_all_four_required_artefacts():
    gate = yaml.safe_load(
        (PACK_ROOT / "acceptance" / "visible" / "GATE-D1-03-contract-compiles-to-project-task-dependency.yaml").read_text()
    )
    evidence = compiled().gate_evidence()
    assert sorted(evidence) == sorted(gate["evidence_required"])
    assert evidence["graph_export_with_edge_types"]["edge_count"] > 0
    assert evidence["critical_path_listing"]["length"] > 0


def test_compilation_produces_no_blocking_findings_against_the_real_pack():
    project = compiled()
    assert project.blocking_findings == [], [f.as_body() for f in project.blocking_findings]
    assert project.compiles is True


def test_every_acceptance_check_compiles_to_a_gate_linked_requirement():
    project = compiled()
    checks = yaml.safe_load((PACK_ROOT / "contract.yaml").read_text())["acceptance_checks"]
    assert len(checks) == 27
    for check in checks:
        requirement_id = project.catalog.by_check.get(check)
        assert requirement_id, f"acceptance check {check!r} compiled to no requirement"
        requirement = project.catalog.get(requirement_id)
        assert requirement.gate_ids, f"{check!r} has no gate"
        assert requirement.gate_files[0]
        assert requirement.criteria, f"{check!r} compiled with no acceptance criteria"
