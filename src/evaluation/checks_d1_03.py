"""GATE-D1-03 — the contract compiles to a project/task/dependency graph.

Contract Sections 8 and 9.6. Five assertions, all five executable today against
the real pack in ``project-pack/``:

    A1 all seventeen ``contract_compiler_outputs`` are produced
    A2 every Task links to at least one Requirement
    A3 the graph is acyclic where the contract requires it
    A4 a critical path is computed and non-empty
    A5 every task carries compiler-selected methodologies

Two rules shaped every check here.

**A manifest is a claim, not a measurement.** The compiler emits an
``efah.compiler_output_manifest`` carrying ``all_seventeen_present``,
``unlinked_tasks``, ``cycles`` and ``critical_path_length``. Reading those
booleans back would test that the compiler agrees with itself. Every check below
therefore recomputes the property from the compiled objects and from the graph,
then cross-checks the manifest against the recomputation -- a manifest claiming
a property its own objects do not support is itself a finding.

**A check that cannot fail is not a check.** Each assertion carries a negative
control that injects the exact defect the assertion forbids and requires the
detector to fire. Where it is affordable the injection goes into a *real*
compilation (a fresh, disposable one -- never the cached one), because a
detector proven on a three-node fixture has not been proven on a graph with 57
tasks and 14 edge types.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contracts.compiler import SECTION_8_OUTPUTS, CompiledProject, compile_pack
from governance.envelope import CompiledObject
from governance.states import DriftFinding
from integrations.pack import ProjectPack, load_pack
from methodologies.applicability import (
    METHODOLOGY_SOURCE,
    ApplicabilityCompiler,
    MethodologyPolicyError,
)
from requirements.graph import DependencyGraph

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext
    from evaluation.gate_spec import AssertionSpec, GateSpec

# ``evaluation.checks`` owns :class:`AssertionOutcome` and the ``ok``/``bad``
# constructors, and this module has to be importable *from* that module so the
# registry can absorb ``CHECKS_D1_03``. Importing checks.py at module scope here
# would turn that registration into a circular import whose failure mode depends
# on which line of checks.py the import happens to sit above. The constructors
# are therefore imported inside each check, where checks.py is guaranteed to be
# fully initialised.

#: Contract Section 8 states seventeen compiler outputs. ``contract.yaml`` names
#: eighteen keys for them, because the first bullet -- "requirement IDs and
#: acceptance criteria" -- is one obligation the YAML splits into
#: ``requirements`` and ``acceptance_criteria``. A1 counts the contract's
#: seventeen; the check below verifies both views and reports the difference
#: rather than resolving it by preference.
SECTION_8_OBLIGATION_COUNT = len(SECTION_8_OUTPUTS)


# ===========================================================================
# Shared subjects
# ===========================================================================


@functools.lru_cache(maxsize=4)
def _pack(repo_root: Path) -> ProjectPack:
    return load_pack(repo_root / "project-pack")


@functools.lru_cache(maxsize=4)
def _compiled(repo_root: Path) -> CompiledProject:
    """The compilation under test. Cached, and therefore never mutated."""
    return compile_pack(_pack(repo_root), repo_root=repo_root)


def _disposable_compilation(repo_root: Path) -> CompiledProject:
    """A fresh compilation for a negative control to vandalise.

    Deliberately uncached: the controls below inject cycles and orphan tasks
    into a real graph, and a mutation that leaked into the cached compilation
    would make a later check fail for a reason that has nothing to do with the
    product.
    """
    return compile_pack(load_pack(repo_root / "project-pack"), repo_root=repo_root)


def _task_objects(project: CompiledProject) -> list[CompiledObject]:
    """The emitted ``efah.task`` objects, not the compiler's in-memory mirror.

    A5 is about what the compiler *produced*, so the task bodies are read from
    the compiled objects that leave the compiler rather than from
    ``CompiledProject.tasks``, which is a convenience copy.
    """
    return [
        obj
        for obj in project.outputs.get("workstreams_milestones_tasks_work_units", [])
        if obj.envelope.schema_id == "efah.task"
    ]


def _findings_of_kind(project: CompiledProject, kind: str) -> list[dict[str, Any]]:
    return [f.as_body() for f in project.findings if f.kind == kind]


# ===========================================================================
# A1 — all seventeen contract_compiler_outputs are produced
# ===========================================================================


def _section_8_presence(
    outputs: Mapping[str, Sequence[Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Recompute Section 8 presence from the emitted objects.

    Both arms of A1 run this one function: the positive arm over the real
    compilation's outputs, the negative control over a copy with one key
    emptied. Sharing it is the point -- a control that exercises a different
    code path from the verdict proves nothing about the verdict.
    """
    presence: dict[str, Any] = {}
    missing: list[str] = []
    for bullet, keys in SECTION_8_OUTPUTS:
        counts = {key: len(outputs.get(key, ())) for key in keys}
        present = all(count > 0 for count in counts.values())
        presence[bullet] = {
            "contract_yaml_keys": list(keys),
            "object_counts": counts,
            "present": present,
        }
        if not present:
            missing.append(bullet)
    return presence, missing


def d1_03_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``output_manifest_check`` -- expected ``all_seventeen_present``."""
    from evaluation.checks import bad, ok

    project = _compiled(ctx.repo_root)
    manifest = project.manifest
    if manifest is None:
        return bad(["the compiler emitted no efah.compiler_output_manifest"])

    body = manifest.body
    presence, missing = _section_8_presence(project.outputs)
    declared_keys = list(body.get("contract_yaml_output_keys", []))
    empty_keys = [key for key in declared_keys if not project.outputs.get(key)]

    # Every contract.yaml key must be reachable from a Section 8 bullet and
    # every mapped key must be declared. Without this, "eighteen keys for
    # seventeen obligations" could conceal an eighteenth obligation nobody
    # mapped: the arity difference is only a labelling difference if the two
    # sets cover each other exactly.
    mapped_keys = {key for _, keys in SECTION_8_OUTPUTS for key in keys}
    unmapped = sorted(set(declared_keys) - mapped_keys)
    undeclared = sorted(mapped_keys - set(declared_keys))

    # The manifest's own claims, checked against the recomputation above.
    manifest_disagreements: list[str] = []
    if body.get("section_8_output_count") != SECTION_8_OBLIGATION_COUNT:
        manifest_disagreements.append(
            f"manifest declares section_8_output_count={body.get('section_8_output_count')!r} "
            f"while the compiler maps {SECTION_8_OBLIGATION_COUNT} Section 8 obligations"
        )
    if bool(body.get("all_seventeen_present")) != (not missing):
        manifest_disagreements.append(
            f"manifest claims all_seventeen_present={body.get('all_seventeen_present')!r} while a "
            f"recomputation from the emitted objects finds missing={missing}"
        )

    # The 18-vs-17 arity is non-blocking, but it must be *declared*. A compiler
    # that quietly reconciled the two counts would be papering over the
    # discrepancy, so the observation's absence is a finding even though its
    # presence is not.
    arity_observations = _findings_of_kind(project, "OUTPUT_LIST_ARITY")
    arity_differs = len(declared_keys) != SECTION_8_OBLIGATION_COUNT
    blocking_output_findings = [
        f.as_body() for f in project.blocking_findings if f.kind == "MISSING_COMPILER_OUTPUT"
    ]

    # Negative control: empty one Section 8 key and require the same
    # recomputation to report that bullet absent.
    victim_bullet, victim_keys = SECTION_8_OUTPUTS[3]  # task_dependencies_and_critical_path
    starved: dict[str, Sequence[Any]] = {k: list(v) for k, v in project.outputs.items()}
    starved[victim_keys[0]] = []
    _, control_missing = _section_8_presence(starved)

    evidence = {
        "compiler_output_manifest": {
            "manifest": body,
            "recomputed_from_emitted_objects": {
                "section_8_obligations": SECTION_8_OBLIGATION_COUNT,
                "section_8_outputs": presence,
                "missing": missing,
                "contract_yaml_output_keys": declared_keys,
                "contract_yaml_output_count": len(declared_keys),
                "contract_yaml_keys_with_no_objects": empty_keys,
                "keys_declared_but_unmapped_to_a_section_8_bullet": unmapped,
                "keys_mapped_but_not_declared_in_contract_yaml": undeclared,
                "manifest_disagreements": manifest_disagreements,
            },
            "output_list_arity": {
                "note": (
                    "contract.yaml lists 18 keys for the 17 Section 8 obligations because the "
                    "first bullet, 'requirement IDs and acceptance criteria', is split into "
                    "'requirements' and 'acceptance_criteria'. Both views are reported and both "
                    "are checked; neither is discarded."
                ),
                "contract_yaml_keys": len(declared_keys),
                "section_8_obligations": SECTION_8_OBLIGATION_COUNT,
                "declared_as_observation_by_the_compiler": arity_observations,
            },
            "compiler_observations": [f.as_body() for f in project.observations],
            "negative_control": {
                "probe": f"emit zero objects for contract.yaml key {victim_keys[0]!r}",
                "detector_reports_missing": control_missing,
                "detector_fires": victim_bullet in control_missing,
            },
        }
    }

    findings: list[str] = []
    findings.extend(f"Section 8 output not produced: {bullet}" for bullet in missing)
    findings.extend(f"contract.yaml key {key!r} produced no compiled object" for key in empty_keys)
    findings.extend(f"contract.yaml key {key!r} maps to no Section 8 bullet" for key in unmapped)
    findings.extend(
        f"a Section 8 bullet maps to key {key!r}, which contract.yaml does not declare"
        for key in undeclared
    )
    findings.extend(manifest_disagreements)
    findings.extend(f"MISSING_COMPILER_OUTPUT: {f['detail']}" for f in blocking_output_findings)
    if arity_differs and not arity_observations:
        findings.append(
            f"contract.yaml declares {len(declared_keys)} compiler outputs against "
            f"{SECTION_8_OBLIGATION_COUNT} Section 8 obligations and the compiler records no "
            "OUTPUT_LIST_ARITY observation; the discrepancy is being reconciled silently"
        )
    if victim_bullet not in control_missing:
        findings.append(
            "negative control did not fire: emptying a Section 8 output still reported it present"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{SECTION_8_OBLIGATION_COUNT}/{SECTION_8_OBLIGATION_COUNT} Section 8 outputs produced, "
            f"recomputed from {sum(len(v) for v in project.outputs.values())} compiled objects. "
            f"contract.yaml lists {len(declared_keys)} keys for those "
            f"{SECTION_8_OBLIGATION_COUNT} obligations -- the first bullet is split in two -- and "
            "every one of them carries objects; the compiler records the arity difference as a "
            "non-blocking OUTPUT_LIST_ARITY observation"
        ),
    )


# ===========================================================================
# A2 — every Task links to at least one Requirement
# ===========================================================================


def d1_03_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A2 ``graph_query_unlinked_tasks`` -- expected ``zero_unlinked_tasks``."""
    from evaluation.checks import bad, ok

    project = _compiled(ctx.repo_root)
    graph = project.graph
    task_nodes = graph.nodes_of_kind("Task")
    unlinked = graph.unlinked_tasks()
    task_objects = _task_objects(project)

    # Two independent readings of "links to a Requirement", because either alone
    # can hold while the assertion is false: a task body can list requirement
    # ids that were never wired into the graph, and a graph edge can exist for a
    # task whose emitted body claims nothing.
    incoming: dict[str, set[str]] = {task_id: set() for task_id in task_nodes}
    for edge in graph.edges_of_type("implemented_by"):
        if graph.nodes[edge.source].kind == "Requirement" and graph.nodes[edge.target].kind == "Task":
            incoming.setdefault(edge.target, set()).add(edge.source)

    requirement_nodes = set(graph.nodes_of_kind("Requirement"))
    bodies_without_requirements: list[str] = []
    body_graph_mismatch: list[str] = []
    for obj in task_objects:
        task_id = str(obj.body.get("task_id"))
        declared = set(obj.body.get("requirement_ids") or [])
        if not declared:
            bodies_without_requirements.append(task_id)
        wired = incoming.get(task_id, set())
        if declared != wired:
            body_graph_mismatch.append(
                f"{task_id}: body declares {sorted(declared - wired)} with no implemented_by edge, "
                f"and the graph carries {sorted(wired - declared)} the body does not declare"
            )
        unknown = sorted(declared - requirement_nodes)
        if unknown:
            body_graph_mismatch.append(f"{task_id}: requirement ids that are not nodes: {unknown}")

    unlinked_findings = _findings_of_kind(project, str(DriftFinding.UNLINKED_TASK))

    # Negative control on a real graph: drop one orphan Task into a fresh
    # compilation of the same pack and require the detector to name it and only
    # it. Injecting among 57 real tasks proves more than a fixture would.
    control = _disposable_compilation(ctx.repo_root)
    control.graph.add_node("TSK-NEGATIVE-CONTROL-ORPHAN", "Task", estimate_units=1)
    control_unlinked = control.graph.unlinked_tasks()
    control_findings = control.graph.unlinked_task_findings()

    evidence = {
        "graph_export_with_edge_types": {
            **graph.export(),
            "task_node_count": len(task_nodes),
            "task_objects_emitted": len(task_objects),
            "unlinked_tasks": unlinked,
            "task_bodies_without_requirement_ids": bodies_without_requirements,
            "body_versus_graph_mismatch": body_graph_mismatch,
            "unlinked_task_findings_from_compilation": unlinked_findings,
            "negative_control": {
                "probe": "add one Task node with no implemented_by edge to a real compiled graph",
                "unlinked_tasks_reported": control_unlinked,
                "finding_kinds": sorted({f["finding"] for f in control_findings}),
                "detector_fires": control_unlinked == ["TSK-NEGATIVE-CONTROL-ORPHAN"],
            },
        }
    }

    findings: list[str] = []
    # A graph with no tasks has no unlinked tasks. That is the adjacent property
    # A2 must never be allowed to pass on, so the task population is part of the
    # verdict rather than a footnote.
    if not task_nodes:
        findings.append("the compiled graph has no Task nodes, so zero unlinked tasks is vacuous")
    if len(task_objects) != len(task_nodes):
        findings.append(
            f"{len(task_nodes)} Task nodes but {len(task_objects)} efah.task objects emitted"
        )
    findings.extend(
        f"{task_id} has no implemented_by edge from any Requirement" for task_id in unlinked
    )
    findings.extend(
        f"{task_id} declares no requirement_ids" for task_id in bodies_without_requirements
    )
    findings.extend(body_graph_mismatch)
    findings.extend(
        f"UNLINKED_TASK recorded at compile time: {f['detail']}" for f in unlinked_findings
    )
    if control_unlinked != ["TSK-NEGATIVE-CONTROL-ORPHAN"]:
        findings.append(
            f"negative control did not fire: an injected orphan task yielded {control_unlinked}"
        )
    if not any(f["finding"] == str(DriftFinding.UNLINKED_TASK) for f in control_findings):
        findings.append("the injected orphan produced no UNLINKED_TASK finding")
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"all {len(task_nodes)} Task nodes carry an implemented_by edge from a Requirement, and "
            f"all {len(task_objects)} emitted efah.task bodies declare exactly the requirement ids "
            "the graph wires to them"
        ),
    )


# ===========================================================================
# A3 — acyclic where the contract requires it
# ===========================================================================

#: The two subgraphs the gate's ``expected`` names. Scanning them is not enough:
#: a scan over a subgraph with no edges is trivially acyclic, which is why A3
#: requires each of these to have been scanned *with edges in it*.
REQUIRED_ACYCLIC_SUBGRAPHS = ("task_depends_on", "role_verified_by")


def _first_same_kind_edge(
    graph: DependencyGraph, edge_type: str, kind: str
) -> tuple[str, str] | None:
    """A real edge between two nodes of one kind, for a control to reverse."""
    for edge in graph.edges_of_type(edge_type):
        if graph.nodes[edge.source].kind == kind and graph.nodes[edge.target].kind == kind:
            return edge.source, edge.target
    return None


def d1_03_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``cycle_detection`` -- expected ``zero_cycles_in_task_and_role_graphs``."""
    from evaluation.checks import bad, ok

    project = _compiled(ctx.repo_root)
    stored = project.cycle_report
    # Recomputed over the graph as it finally stands. If the compiler stored a
    # report and then kept adding edges, the stored report describes an older
    # graph and A3 would be passing on history.
    recomputed = project.graph.cycles()

    scanned = dict(recomputed.scanned)
    vacuous = [label for label in REQUIRED_ACYCLIC_SUBGRAPHS if scanned.get(label, 0) <= 0]
    circular_findings = _findings_of_kind(project, "CIRCULAR_DEPENDENCY")

    # Negative control, injected into a real compiled graph: reverse one real
    # task dependency and one real role verification, so each subgraph the gate
    # names contains a genuine cycle, then rescan.
    control = _disposable_compilation(ctx.repo_root)
    control_graph = control.graph
    task_edge = _first_same_kind_edge(control_graph, "depends_on", "Task")
    role_edge = _first_same_kind_edge(control_graph, "verified_by", "Role")
    injected: list[str] = []
    if task_edge:
        control_graph.add_edge(
            task_edge[1],
            task_edge[0],
            "depends_on",
            dependency_class="task",
            rationale="negative control: reversed a real task dependency",
        )
        injected.append(f"depends_on {task_edge[1]} -> {task_edge[0]}")
    if role_edge:
        control_graph.add_edge(
            role_edge[1],
            role_edge[0],
            "verified_by",
            dependency_class="task",
            rationale="negative control: circular role validation, Section 12.2",
        )
        injected.append(f"verified_by {role_edge[1]} -> {role_edge[0]}")
    control_report = control_graph.cycles()
    labels_caught = sorted({cycle[0] for cycle in control_report.cycles})

    evidence = {
        "cycle_detection_report": {
            "stored_at_compile_time": stored.as_body(),
            "recomputed_from_the_final_graph": recomputed.as_body(),
            "subgraphs_required_acyclic": list(REQUIRED_ACYCLIC_SUBGRAPHS),
            "edges_scanned_per_subgraph": scanned,
            "circular_dependency_findings": circular_findings,
            "negative_control": {
                "probe": "reverse one real task dependency and one real role verification",
                "injected": injected,
                "cycles_detected": control_report.cycles,
                "subgraph_labels_caught": labels_caught,
                "acyclic_after_injection": control_report.acyclic,
            },
        }
    }

    findings: list[str] = []
    findings.extend(f"cycle in {cycle[0]}: {' -> '.join(cycle[1:])}" for cycle in recomputed.cycles)
    if stored.as_body() != recomputed.as_body():
        findings.append(
            "the cycle report stored at compile time does not match a recomputation over the "
            f"final graph: stored {stored.as_body()}, recomputed {recomputed.as_body()}"
        )
    findings.extend(
        f"{label} was scanned with {scanned.get(label, 0)} edges, so 'acyclic' there is vacuous"
        for label in vacuous
    )
    findings.extend(
        f"CIRCULAR_DEPENDENCY recorded at compile time: {f['detail']}" for f in circular_findings
    )
    if not task_edge or not role_edge:
        findings.append(
            "the negative control could not be built: the graph carries no Task->Task depends_on "
            "edge or no Role->Role verified_by edge to reverse"
        )
    else:
        missed = [label for label in REQUIRED_ACYCLIC_SUBGRAPHS if label not in labels_caught]
        if missed:
            findings.append(f"negative control did not fire for {missed}: injected cycles went undetected")
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            "zero cycles over "
            + ", ".join(
                f"{label} ({scanned.get(label, 0)} edges)" for label in REQUIRED_ACYCLIC_SUBGRAPHS
            )
            + f"; cycles injected into both subgraphs were caught ({labels_caught})"
        ),
    )


# ===========================================================================
# A4 — a critical path is computed and non-empty
# ===========================================================================


def d1_03_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``critical_path_extract`` -- expected ``path_length > 0``."""
    from evaluation.checks import bad, ok

    project = _compiled(ctx.repo_root)
    graph = project.graph
    stored = project.critical_path
    recomputed = graph.critical_path()

    # "Non-empty" is the letter of the assertion, and a list of node ids that is
    # not a walk in the dependency graph would satisfy the letter while meaning
    # nothing. Every consecutive pair is therefore checked against a real
    # depends_on edge in the direction the graph records it -- the path is
    # returned in execution order, prerequisite first, so the edge runs the
    # other way.
    depends_on = {(edge.source, edge.target) for edge in graph.edges_of_type("depends_on")}
    broken_links = [
        f"{dependent} does not depend_on {prerequisite}"
        for prerequisite, dependent in zip(stored.nodes, stored.nodes[1:], strict=False)
        if (dependent, prerequisite) not in depends_on
    ]
    non_task_nodes = [
        n for n in stored.nodes if n not in graph.nodes or graph.nodes[n].kind != "Task"
    ]
    empty_path_findings = _findings_of_kind(project, "EMPTY_CRITICAL_PATH")

    # Two negative controls. The first shows the extractor returns zero rather
    # than a default when there is nothing to extract. The second is the one
    # that matters: on a real graph made cyclic, the extractor must refuse
    # instead of fabricating, because a fabricated path would let A4 pass on a
    # graph A3 rejects.
    empty_path = DependencyGraph().critical_path()
    control = _disposable_compilation(ctx.repo_root)
    task_edge = _first_same_kind_edge(control.graph, "depends_on", "Task")
    if task_edge:
        control.graph.add_edge(
            task_edge[1],
            task_edge[0],
            "depends_on",
            dependency_class="task",
            rationale="negative control: cyclic task graph",
        )
    cyclic_path = control.graph.critical_path()

    evidence = {
        "critical_path_listing": {
            "stored_at_compile_time": stored.as_body(),
            "recomputed_from_the_final_graph": recomputed.as_body(),
            "task_titles": [project.tasks[n]["title"] for n in stored.nodes if n in project.tasks],
            "every_hop_is_a_real_depends_on_edge": not broken_links,
            "broken_links": broken_links,
            "path_nodes_that_are_not_tasks": non_task_nodes,
            "empty_critical_path_findings": empty_path_findings,
            "negative_control": {
                "empty_graph": {
                    "probe": "extract a critical path from a graph with no nodes",
                    "length": empty_path.length,
                    "path": empty_path.nodes,
                },
                "cyclic_real_graph": {
                    "probe": "reverse one real task dependency, making the task graph cyclic",
                    "injected": f"{task_edge[1]} -> {task_edge[0]}" if task_edge else None,
                    "length": cyclic_path.length,
                    "path": cyclic_path.nodes,
                },
            },
        }
    }

    findings: list[str] = []
    if stored.length <= 0 or not stored.nodes:
        findings.append(f"the critical path is empty: length={stored.length}, nodes={stored.nodes}")
    if stored.length != len(stored.nodes):
        findings.append(
            f"critical path length {stored.length} disagrees with its {len(stored.nodes)} nodes"
        )
    if stored.as_body() != recomputed.as_body():
        findings.append(
            "the critical path stored at compile time does not match a recomputation over the "
            f"final graph: stored {stored.nodes}, recomputed {recomputed.nodes}"
        )
    if stored.weight <= 0:
        findings.append(f"the critical path carries no weight: {stored.weight}")
    findings.extend(broken_links)
    findings.extend(f"critical path node {n!r} is not a Task node" for n in non_task_nodes)
    findings.extend(
        f"EMPTY_CRITICAL_PATH recorded at compile time: {f['detail']}" for f in empty_path_findings
    )
    if empty_path.length != 0:
        findings.append(
            f"negative control did not fire: an empty graph yielded length {empty_path.length}"
        )
    if not task_edge:
        findings.append(
            "the negative control could not be built: no Task->Task depends_on edge to reverse"
        )
    elif cyclic_path.length != 0:
        findings.append(
            "negative control did not fire: a cyclic task graph still yielded a path of length "
            f"{cyclic_path.length}"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"critical path of {stored.length} tasks, weight {stored.weight}, every hop a real "
            f"depends_on edge: {' -> '.join(stored.nodes)}. The same extractor returns length 0 on "
            "an empty graph and on a cyclic real graph"
        ),
    )


# ===========================================================================
# A5 — compiler-selected methodologies, not agent-chosen ones
# ===========================================================================


def _methodology_findings(
    task_bodies: Sequence[Mapping[str, Any]],
    *,
    applicability: ApplicabilityCompiler,
    catalog_ids: set[str],
) -> list[str]:
    """The predicate behind A5, run over whatever task bodies it is handed.

    Provenance is not a string comparison. ``methodology_source`` is a field
    anything can type into a body; what makes a task's methodologies
    *compiler-selected* is that re-running the Section 13.3 applicability
    compiler over that task's own class and risk reproduces the exact set. That
    reproduction is what this function performs, which is why the control below
    can flip a body to ``agent_selected`` and be caught for the right reason.
    """
    findings: list[str] = []
    for body in task_bodies:
        task_id = str(body.get("task_id"))
        source = body.get("methodology_source")
        selected = list(body.get("methodology_ids") or [])
        if source != METHODOLOGY_SOURCE:
            findings.append(
                f"{task_id}: methodology_source is {source!r}, not {METHODOLOGY_SOURCE!r}"
            )
        if not selected:
            findings.append(f"{task_id}: carries no methodology at all")
        outside = sorted(set(selected) - catalog_ids)
        if outside:
            findings.append(f"{task_id}: methodologies outside the owner catalog: {outside}")
        try:
            reproduced = applicability.select(
                task_id=task_id,
                task_class=str(body.get("task_class")),
                risk=str(body.get("risk_class")),
            )
        except MethodologyPolicyError as exc:
            findings.append(f"{task_id}: no applicability rule reproduces its selection ({exc})")
            continue
        if reproduced.methodology_ids != selected:
            findings.append(
                f"{task_id}: carries {selected} but the applicability compiler selects "
                f"{reproduced.methodology_ids} for task_class={body.get('task_class')!r} "
                f"risk={body.get('risk_class')!r}"
            )
        if reproduced.methodology_source != METHODOLOGY_SOURCE:
            findings.append(
                f"{task_id}: the applicability compiler stamped {reproduced.methodology_source!r}"
            )
    return findings


def d1_03_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A5 ``methodology_provenance_check`` -- expected ``methodology_source == applicability_compiler``."""
    from evaluation.checks import bad, ok

    project = _compiled(ctx.repo_root)
    applicability = ApplicabilityCompiler(_pack(ctx.repo_root))
    catalog_ids = set(applicability.catalog)
    task_bodies = [obj.body for obj in _task_objects(project)]

    findings = _methodology_findings(
        task_bodies, applicability=applicability, catalog_ids=catalog_ids
    )
    provenance_findings = _findings_of_kind(project, "METHODOLOGY_PROVENANCE")

    # Negative control 1: three forged bodies, each a way an agent-chosen set
    # could reach a task -- a foreign source string, no methodologies at all,
    # and a substituted set drawn from the owner catalog so it *looks* compiled.
    if task_bodies:
        template = dict(task_bodies[0])
        forged = {**template, "methodology_source": "agent_selected"}
        stripped = {**template, "methodology_ids": []}
        substituted = {**template, "methodology_ids": sorted(catalog_ids)[:1]}
        control_findings = _methodology_findings(
            [forged, stripped, substituted],
            applicability=applicability,
            catalog_ids=catalog_ids,
        )
        control_caught = {
            "foreign_source": any("methodology_source is" in f for f in control_findings),
            "no_methodology": any("carries no methodology" in f for f in control_findings),
            "substituted_set": any("but the applicability compiler selects" in f for f in control_findings),
        }
    else:
        control_findings = []
        control_caught = {"foreign_source": False, "no_methodology": False, "substituted_set": False}

    # Negative control 2: the selector must refuse an unmapped task class rather
    # than defaulting. A silent default is precisely how an unrecognised class
    # would end up carrying methodologies nobody selected for it.
    try:
        applicability.select(task_id="TSK-NEGATIVE-CONTROL", task_class="vibes", risk="high")
        refused_unknown_class = False
        refusal_detail = "an unmapped task class was served a default selection"
    except MethodologyPolicyError as exc:
        refused_unknown_class = True
        refusal_detail = str(exc)

    evidence = {
        "methodology_provenance_report": {
            "expected_methodology_source": METHODOLOGY_SOURCE,
            "task_objects_checked": len(task_bodies),
            "methodology_catalog_version": applicability.catalog_version,
            "owner_catalog_size": len(catalog_ids),
            "sources_observed": sorted({str(b.get("methodology_source")) for b in task_bodies}),
            "every_selection_reproduced_by_the_applicability_compiler": not findings,
            "compile_time_provenance_findings": provenance_findings,
            "negative_control": {
                "probe": (
                    "three forged task bodies: methodology_source=agent_selected, no "
                    "methodologies, and a substituted catalog-legal set"
                ),
                "detector_findings": control_findings,
                "detector_caught": control_caught,
                "unmapped_task_class_refused": refused_unknown_class,
                "refusal_detail": refusal_detail,
            },
        }
    }

    all_findings = list(findings)
    all_findings.extend(
        f"METHODOLOGY_PROVENANCE recorded at compile time: {f['detail']}"
        for f in provenance_findings
    )
    if not task_bodies:
        all_findings.append(
            "no efah.task objects were emitted, so methodology provenance is vacuous"
        )
    missed_controls = sorted(name for name, caught in control_caught.items() if not caught)
    if missed_controls:
        all_findings.append(
            f"negative control did not fire for {missed_controls}: forged task bodies produced "
            f"{control_findings}"
        )
    if not refused_unknown_class:
        all_findings.append(refusal_detail)
    if all_findings:
        return bad(all_findings, evidence)
    return ok(
        evidence,
        (
            f"all {len(task_bodies)} emitted tasks carry methodology_source={METHODOLOGY_SOURCE!r}, "
            "and re-running the Section 13.3 applicability compiler over each task's own class and "
            "risk reproduces its exact methodology set from the owner catalog "
            f"(v{applicability.catalog_version})"
        ),
    )


# ===========================================================================
# Registry
# ===========================================================================

CHECKS_D1_03: dict[tuple[str, str], Check] = {
    ("GATE-D1-03", "A1"): d1_03_a1,
    ("GATE-D1-03", "A2"): d1_03_a2,
    ("GATE-D1-03", "A3"): d1_03_a3,
    ("GATE-D1-03", "A4"): d1_03_a4,
    ("GATE-D1-03", "A5"): d1_03_a5,
}
