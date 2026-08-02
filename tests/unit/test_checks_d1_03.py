"""The GATE-D1-03 checks, tested the only way a check is worth testing.

Every assertion gets two tests: it PASSes against the real pack, and it FAILs
against a compilation with the exact defect the assertion forbids injected into
it. The second test is the one that matters. A check that only ever sees a
healthy subject can be a constant function returning PASS and nobody would
notice -- which is how a gate becomes decorative.

The broken subjects here are real compilations of the real pack, vandalised
after the fact, not hand-built fixtures. A detector proven against a three-node
fixture has not been proven against the graph the gate actually decides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.compiler import compile_pack
from evaluation import checks_d1_03
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d1_03 import CHECKS_D1_03
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import load_all_gates
from governance.states import Verdict
from integrations.pack import load_pack
from methodologies.applicability import METHODOLOGY_SOURCE
from requirements.graph import CriticalPath, DependencyGraph

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_ID = "GATE-D1-03"


@pytest.fixture(scope="module")
def gates():
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates):
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates):
    return GateContext(
        binding=CandidateBinding.from_head(repo_root=REPO_ROOT),
        gates=gates,
        repo_root=REPO_ROOT,
    )


def run(assertion_id: str, ctx, gate):
    """Invoke a check exactly as the runner does: (ctx, gate, assertion)."""
    assertion = next(a for a in gate.assertions if a.assertion_id == assertion_id)
    return CHECKS_D1_03[(GATE_ID, assertion_id)](ctx, gate, assertion)


def vandalised():
    """A real compilation, freshly made so a test can break it in isolation."""
    return compile_pack(load_pack(REPO_ROOT / "project-pack"), repo_root=REPO_ROOT)


@pytest.fixture
def broken(monkeypatch):
    """Substitute a vandalised compilation for the subject under test."""
    project = vandalised()
    monkeypatch.setattr(checks_d1_03, "_compiled", lambda repo_root: project)
    return project


def task_object(project, task_id: str | None = None):
    objects = checks_d1_03._task_objects(project)
    if task_id is None:
        return objects[0]
    return next(o for o in objects if o.body["task_id"] == task_id)


# ==========================================================================
# Registration — the runner must be able to find and call these
# ==========================================================================


def test_the_registry_covers_every_assertion_the_gate_declares(gate):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {assertion_id for gate_id, assertion_id in CHECKS_D1_03 if gate_id == GATE_ID}
    assert registered == declared == {"A1", "A2", "A3", "A4", "A5"}
    assert all(callable(fn) for fn in CHECKS_D1_03.values())


def test_the_gate_runs_end_to_end_once_the_checks_are_registered(monkeypatch, gates):
    """Registration is the point of the module, so it is exercised, not assumed.

    ``evidence_required`` is the reason this test is not redundant with the
    five below: the runner downgrades a gate to UNVERIFIABLE when a named
    evidence artifact was never produced, so five passing assertions that
    forgot an artifact would still not be a passing gate.
    """
    for key, check in CHECKS_D1_03.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    summary = runner.run([GATE_ID])
    result = summary.results[0]
    assert result.executability is Executability.EXECUTED
    assert result.evidence_missing == []
    assert set(result.evidence_required) <= runner.evidence.names_for(GATE_ID)
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]


# ==========================================================================
# A1 — all seventeen contract_compiler_outputs are produced
# ==========================================================================


def test_a1_passes_on_the_real_pack(ctx, gate):
    outcome = run("A1", ctx, gate)
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    recomputed = outcome.evidence["compiler_output_manifest"]["recomputed_from_emitted_objects"]
    assert recomputed["section_8_obligations"] == 17
    assert recomputed["missing"] == []
    assert recomputed["contract_yaml_keys_with_no_objects"] == []
    assert outcome.evidence["compiler_output_manifest"]["negative_control"]["detector_fires"] is True


def test_a1_reports_the_eighteen_key_arity_rather_than_hiding_it(ctx, gate):
    """17 obligations, 18 keys. The check must say so, in evidence and in prose."""
    outcome = run("A1", ctx, gate)
    arity = outcome.evidence["compiler_output_manifest"]["output_list_arity"]
    assert arity["contract_yaml_keys"] == 18
    assert arity["section_8_obligations"] == 17
    assert arity["declared_as_observation_by_the_compiler"], "the compiler stopped declaring it"
    assert "18 keys" in outcome.note


def test_a1_fails_when_a_section_8_output_is_not_produced(broken, ctx, gate):
    broken.outputs["oracle_routes"] = []
    outcome = run("A1", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("oracle_routes" in f for f in outcome.findings)


def test_a1_fails_when_the_manifest_claims_more_than_the_objects_support(broken, ctx, gate):
    """The manifest is a claim. Emptying an output while leaving the claim
    intact is exactly the shape of a fabricated green, and A1 recomputes rather
    than reading ``all_seventeen_present`` back."""
    broken.outputs["completion_conditions"] = []
    broken.manifest.body["all_seventeen_present"] = True
    outcome = run("A1", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("all_seventeen_present" in f for f in outcome.findings)


def test_a1_fails_when_the_arity_difference_is_reconciled_silently(broken, ctx, gate):
    broken.findings = [f for f in broken.findings if f.kind != "OUTPUT_LIST_ARITY"]
    outcome = run("A1", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("OUTPUT_LIST_ARITY" in f for f in outcome.findings)


# ==========================================================================
# A2 — every Task links to at least one Requirement
# ==========================================================================


def test_a2_passes_on_the_real_pack(ctx, gate):
    outcome = run("A2", ctx, gate)
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    export = outcome.evidence["graph_export_with_edge_types"]
    assert export["unlinked_tasks"] == []
    assert export["task_node_count"] > 0
    assert export["negative_control"]["detector_fires"] is True
    assert "UNLINKED_TASK" in export["negative_control"]["finding_kinds"]


def test_a2_fails_when_a_task_links_to_no_requirement(broken, ctx, gate):
    broken.graph.add_node("TSK-INJECTED-ORPHAN", "Task", estimate_units=1)
    outcome = run("A2", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("TSK-INJECTED-ORPHAN" in f for f in outcome.findings)


def test_a2_fails_when_a_task_body_claims_a_link_the_graph_does_not_carry(broken, ctx, gate):
    """A requirement id typed into a body is not a link. The edge is the link."""
    task_object(broken).body["requirement_ids"] = ["REQ-DOES-NOT-EXIST"]
    outcome = run("A2", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("REQ-DOES-NOT-EXIST" in f for f in outcome.findings)


def test_a2_fails_on_a_graph_with_no_tasks_at_all(broken, ctx, gate):
    """Zero unlinked tasks is trivially true of an empty graph."""
    broken.graph = DependencyGraph()
    broken.outputs["workstreams_milestones_tasks_work_units"] = []
    outcome = run("A2", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("vacuous" in f for f in outcome.findings)


# ==========================================================================
# A3 — acyclic where the contract requires it
# ==========================================================================


def test_a3_passes_on_the_real_pack(ctx, gate):
    outcome = run("A3", ctx, gate)
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = outcome.evidence["cycle_detection_report"]
    assert report["recomputed_from_the_final_graph"]["cycles"] == []
    assert report["edges_scanned_per_subgraph"]["task_depends_on"] > 0
    assert report["edges_scanned_per_subgraph"]["role_verified_by"] > 0
    assert report["negative_control"]["subgraph_labels_caught"] == [
        "role_verified_by",
        "task_depends_on",
    ]


def test_a3_fails_on_a_task_dependency_cycle(broken, ctx, gate):
    edge = checks_d1_03._first_same_kind_edge(broken.graph, "depends_on", "Task")
    broken.graph.add_edge(edge[1], edge[0], "depends_on", dependency_class="task")
    broken.cycle_report = broken.graph.cycles()
    outcome = run("A3", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any(f.startswith("cycle in task_depends_on") for f in outcome.findings)


def test_a3_fails_on_circular_role_validation(broken, ctx, gate):
    """Section 12.2: a role that transitively validates its own producer."""
    edge = checks_d1_03._first_same_kind_edge(broken.graph, "verified_by", "Role")
    broken.graph.add_edge(edge[1], edge[0], "verified_by", dependency_class="task")
    broken.cycle_report = broken.graph.cycles()
    outcome = run("A3", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any(f.startswith("cycle in role_verified_by") for f in outcome.findings)


def test_a3_fails_when_a_required_subgraph_was_never_really_scanned(broken, ctx, gate):
    """An acyclic scan over zero edges is a scan that decided nothing."""
    empty = DependencyGraph()
    empty.add_node("TSK-A", "Task")
    empty.add_node("TSK-B", "Task")
    empty.add_edge("TSK-A", "TSK-B", "depends_on", dependency_class="task")
    broken.graph = empty
    broken.cycle_report = empty.cycles()
    outcome = run("A3", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("role_verified_by" in f and "vacuous" in f for f in outcome.findings)


def test_a3_fails_when_the_stored_report_describes_an_older_graph(broken, ctx, gate):
    """A stale report would let a cycle added after compilation go unseen."""
    edge = checks_d1_03._first_same_kind_edge(broken.graph, "depends_on", "Task")
    broken.graph.add_edge(edge[1], edge[0], "depends_on", dependency_class="task")
    # cycle_report deliberately left as compiled: it still says "acyclic".
    assert broken.cycle_report.acyclic is True
    outcome = run("A3", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not match a recomputation" in f for f in outcome.findings)


# ==========================================================================
# A4 — a critical path is computed and non-empty
# ==========================================================================


def test_a4_passes_on_the_real_pack(ctx, gate):
    outcome = run("A4", ctx, gate)
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    listing = outcome.evidence["critical_path_listing"]
    assert listing["stored_at_compile_time"]["length"] > 0
    assert listing["every_hop_is_a_real_depends_on_edge"] is True
    assert listing["negative_control"]["empty_graph"]["length"] == 0
    assert listing["negative_control"]["cyclic_real_graph"]["length"] == 0


def test_a4_fails_when_no_critical_path_could_be_computed(broken, ctx, gate):
    broken.graph = DependencyGraph()
    broken.critical_path = CriticalPath()
    broken.tasks = {}
    outcome = run("A4", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("critical path is empty" in f for f in outcome.findings)


def test_a4_fails_on_a_non_empty_path_that_is_not_a_path(broken, ctx, gate):
    """``path_length > 0`` is satisfiable by a list of ids nobody can walk."""
    tasks = broken.graph.nodes_of_kind("Task")
    broken.critical_path = CriticalPath(nodes=[tasks[0], tasks[-1]], length=2, weight=2.0)
    outcome = run("A4", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not depend_on" in f for f in outcome.findings)


def test_a4_fails_when_the_task_graph_became_cyclic(broken, ctx, gate):
    edge = checks_d1_03._first_same_kind_edge(broken.graph, "depends_on", "Task")
    broken.graph.add_edge(edge[1], edge[0], "depends_on", dependency_class="task")
    outcome = run("A4", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not match a recomputation" in f for f in outcome.findings)


# ==========================================================================
# A5 — compiler-selected methodologies, not agent-chosen ones
# ==========================================================================


def test_a5_passes_on_the_real_pack(ctx, gate):
    outcome = run("A5", ctx, gate)
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = outcome.evidence["methodology_provenance_report"]
    assert report["sources_observed"] == [METHODOLOGY_SOURCE]
    assert report["every_selection_reproduced_by_the_applicability_compiler"] is True
    assert all(report["negative_control"]["detector_caught"].values())
    assert report["negative_control"]["unmapped_task_class_refused"] is True


def test_a5_fails_when_a_task_declares_an_agent_chosen_source(broken, ctx, gate):
    task_object(broken).body["methodology_source"] = "agent_selected"
    outcome = run("A5", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("agent_selected" in f for f in outcome.findings)


def test_a5_fails_when_a_task_carries_no_methodology(broken, ctx, gate):
    task_object(broken).body["methodology_ids"] = []
    outcome = run("A5", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("carries no methodology" in f for f in outcome.findings)


def test_a5_fails_on_a_substituted_set_that_still_claims_compiler_provenance(broken, ctx, gate):
    """The subtle one: catalog-legal methods, correct source string, wrong set.

    Only re-running the applicability compiler over the task's own class and
    risk catches this, which is why A5 does that instead of comparing strings.
    """
    obj = task_object(broken)
    assert obj.body["methodology_source"] == METHODOLOGY_SOURCE
    catalog = sorted(checks_d1_03.ApplicabilityCompiler(load_pack(REPO_ROOT / "project-pack")).catalog)
    substitute = next(m for m in catalog if m not in obj.body["methodology_ids"])
    obj.body["methodology_ids"] = [substitute]
    outcome = run("A5", ctx, gate)
    assert outcome.status is AssertionStatus.FAIL
    assert any("the applicability compiler selects" in f for f in outcome.findings)
