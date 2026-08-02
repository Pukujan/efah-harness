"""The contract compiler.

Contract EFAH-CONTRACT-001 v1.1 Section 8: transform the approved contract into
machine-checkable output. Section 8.1: every compiled object carries the
envelope. Section 8.1 phase matrix, "Project compilation": the graph must be
acyclic where required and every task must link to a requirement, or compilation
fails.

Acceptance gate: ``GATE-D1-03-contract-compiles-to-project-task-dependency``.

    A1 all seventeen contract_compiler_outputs are produced
    A2 every Task links to at least one Requirement
    A3 zero cycles in the task and role graphs
    A4 a critical path is computed and non-empty
    A5 every task carries compiler-selected methodologies

Everything here is a pure function of the project pack plus the two owner
documents the pack points at (AMENDMENT-001 and DEC-005). No model is called: a
deterministic gate's verdict path may not contain a judge (Section 17.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import markdown
from contracts.plan import PLAN_ITEMS, PlanItem, PlanValidation, validate_against_pack
from governance.compiler import CompilationError, emit
from governance.envelope import CONTRACT_VERSION, CompiledObject, EvidenceTier, KnowledgeTier, content_hash
from governance.states import (
    TERMINAL_PROJECT_STATES,
    WORKER_SUBMITTABLE_STATES,
    DriftFinding,
    FailureClass,
    OwnerInterrupt,
    ProjectState,
    TaskState,
)
from impact.revalidation import AmendmentRecompilation, recompile
from integrations.pack import ProjectPack
from methodologies.applicability import ApplicabilityCompiler, MethodologySelection
from requirements.catalog import CompilationFinding, RequirementCatalog, build_catalog, load_acceptance_index
from requirements.graph import CriticalPath, CycleReport, DependencyGraph

#: Contract Section 8's seventeen bullets, mapped onto the ``contract.yaml``
#: keys that realise them. The first bullet ("requirement IDs and acceptance
#: criteria") is one obligation that ``contract.yaml`` splits into two keys,
#: which is why the YAML lists eighteen names for seventeen outputs. GATE-D1-03
#: A1 counts the contract's seventeen; the manifest reports both views so the
#: discrepancy is visible rather than resolved by preference.
SECTION_8_OUTPUTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("requirement_ids_and_acceptance_criteria", ("requirements", "acceptance_criteria")),
    ("phase_definitions_and_allowed_transitions", ("phases_and_transitions",)),
    ("workstreams_milestones_tasks_and_work_units", ("workstreams_milestones_tasks_work_units",)),
    ("task_dependencies_and_critical_path", ("dependencies_and_critical_path",)),
    ("required_methodologies_by_task_and_risk_class", ("methodologies_by_task_and_risk",)),
    ("role_definitions_and_incompatibility_rules", ("role_separation",)),
    ("model_capability_requirements", ("model_capability_requirements",)),
    ("artifact_schemas", ("artifact_schemas",)),
    ("allowed_and_prohibited_repository_paths", ("allowed_and_prohibited_paths",)),
    ("source_and_evidence_rules", ("source_and_evidence_rules",)),
    ("visible_and_hidden_test_obligations", ("visible_and_hidden_test_obligations",)),
    ("oracle_routes", ("oracle_routes",)),
    ("success_and_failure_conditions", ("success_and_failure_conditions",)),
    ("contract_re_review_triggers", ("contract_review_triggers",)),
    ("auto_merge_conditions", ("auto_merge_conditions",)),
    ("human_escalation_conditions", ("human_escalation_conditions",)),
    ("completion_conditions", ("completion_conditions",)),
)

#: Section 12.2 / 12.4 / 14.3 / 17.1: producer -> validator. Direction matters:
#: a cycle here is the circular validation Section 12.2 forbids.
ROLE_VALIDATION: tuple[tuple[str, str, str], ...] = (
    ("researcher", "research_challenger", "contract.md#7.4,#12.1"),
    ("planner", "plan_challenger", "contract.md#12.1"),
    ("implementer", "visible_test_author", "contract.md#14.3"),
    ("implementer", "integration_verifier", "contract.md#5.2"),
    ("implementer", "adversarial_critic", "contract.md#12.2 producer_not_sole_reviewer"),
    ("implementer", "sealed_holdout_author", "contract.md#12.2 builder_ne_holdout_author"),
    ("implementer", "mutant_author", "contract.md#17.1"),
    ("implementer", "oracle_author", "contract.md#17.4"),
    ("implementer", "judge", "contract.md#12.2 builder_ne_final_adjudicator"),
    ("implementer", "evidence_auditor", "contract.md#18"),
    ("implementer", "contract_compliance_auditor", "contract.md#1.2"),
    ("implementer", "release_verifier", "contract.md#21"),
    ("adversarial_critic", "judge", "contract.md#12.4 produce_critique_adjudicate"),
)

#: Section 9.4 failure conditions. Verbatim from the contract's schema block.
WORK_UNIT_FAILURE_CONDITIONS: tuple[str, ...] = (
    "stale_contract_version",
    "protected_asset_access",
    "unauthorized_scope",
    "missing_wiring",
    "fabricated_evidence",
    "unsupported_dependency_reimplementation",
)

#: Which selected dependency each plan item integrates (Section 14.2: integrate,
#: do not rebuild). Used to emit software-package dependency edges.
PLAN_ITEM_PACKAGES: dict[str, tuple[str, ...]] = {
    "terminusdb_schemas": ("authoritative_graph",),
    "langgraph_skeletons": ("workflow_runtime", "checkpoint_store"),
    "plane_projection": ("pm_projection",),
    "context7_cache": ("documentation",),
    "model_router_path": ("model_gateway",),
    "rag_ingestion_and_retrieval": ("document_ingestion", "retrieval_index", "rag_components"),
    "inspect_and_protected_verifier": ("evaluation_runtime",),
    "visible_hidden_mutation_oracle_lane": ("adversarial_evaluation",),
    "observability": ("tracing", "experiments"),
    "owner_control_surface": ("api", "workflow_runtime"),
    "first_walking_skeleton": ("api", "schemas"),
}

#: Which environment service each plan item needs live (Section 9.6 service
#: dependencies). Keys are ``environments.yaml -> environments.dev`` entries.
PLAN_ITEM_SERVICES: dict[str, tuple[str, ...]] = {
    "terminusdb_schemas": ("terminusdb", "terminusdb_protected"),
    "plane_projection": ("plane",),
    "model_router_path": ("litellm_production", "litellm_eval"),
    "context7_cache": ("context7",),
    "observability": ("phoenix",),
    "owner_control_surface": ("litellm_production",),
    "first_walking_skeleton": ("terminusdb", "litellm_production", "plane"),
}


@dataclass
class CompiledProject:
    """Everything Section 8 requires, sealed and hashed."""

    project_id: str
    contract_id: str
    contract_version: str
    pack_manifest_hash: str
    outputs: dict[str, list[CompiledObject]] = field(default_factory=dict)
    graph: DependencyGraph = field(default_factory=DependencyGraph)
    cycle_report: CycleReport = field(default_factory=CycleReport)
    critical_path: CriticalPath = field(default_factory=CriticalPath)
    catalog: RequirementCatalog = field(default_factory=RequirementCatalog)
    findings: list[CompilationFinding] = field(default_factory=list)
    recompilation: AmendmentRecompilation | None = None
    plan_validation: PlanValidation | None = None
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest: CompiledObject | None = None
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    methodology_selections: dict[str, MethodologySelection] = field(default_factory=dict)

    # -- accessors ---------------------------------------------------------

    @property
    def all_objects(self) -> list[CompiledObject]:
        objects = [obj for group in self.outputs.values() for obj in group]
        if self.manifest is not None:
            objects.append(self.manifest)
        return objects

    @property
    def object_count(self) -> int:
        return len(self.all_objects)

    @property
    def blocking_findings(self) -> list[CompilationFinding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def observations(self) -> list[CompilationFinding]:
        return [f for f in self.findings if f.severity != "blocking"]

    @property
    def compiles(self) -> bool:
        return (
            not self.blocking_findings
            and self.cycle_report.acyclic
            and not self.graph.unlinked_tasks()
            and self.critical_path.length > 0
        )

    @property
    def terminal_state(self) -> ProjectState:
        return ProjectState.RUNNING if self.compiles else ProjectState.FAILED_CONTRACT

    def counts(self) -> dict[str, int]:
        return {name: len(objs) for name, objs in sorted(self.outputs.items())}

    def gate_evidence(self) -> dict[str, Any]:
        """The four artefacts GATE-D1-03 lists under ``evidence_required``."""
        return {
            "compiler_output_manifest": self.manifest.body if self.manifest else {},
            "graph_export_with_edge_types": self.graph.export(),
            "cycle_detection_report": self.cycle_report.as_body(),
            "critical_path_listing": self.critical_path.as_body(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "pack_manifest_hash": self.pack_manifest_hash,
            "compiled_object_count": self.object_count,
            "requirements": len(self.catalog.requirements),
            "acceptance_criteria": len(self.catalog.criteria),
            "tasks": len(self.graph.nodes_of_kind("Task")),
            "work_units": len(self.graph.nodes_of_kind("WorkUnit")),
            "dependency_edges": len(self.graph.edges),
            "edges_by_type": self.graph.edge_type_counts(),
            "critical_path_length": self.critical_path.length,
            "critical_path": self.critical_path.nodes,
            "cycles": len(self.cycle_report.cycles),
            "unlinked_tasks": self.graph.unlinked_tasks(),
            "outputs": self.counts(),
            "blocking_findings": [f.as_body() for f in self.blocking_findings],
            "observations": [f.as_body() for f in self.observations],
            "compiles": self.compiles,
        }


class ContractCompiler:
    """Compile a validated :class:`ProjectPack` into Section 8's outputs."""

    def __init__(self, pack: ProjectPack, *, repo_root: Path | None = None) -> None:
        self.pack = pack
        self.repo_root = repo_root or pack.root.parent
        self.contract = pack.yaml("contract.yaml")
        self.project = pack.yaml("project.yaml")
        self.model_policy = pack.yaml("model-policy.yaml")
        self.autonomy = pack.yaml("autonomy-policy.yaml")
        self.dependency_policy = pack.yaml("dependency-policy.yaml")
        self.environments = pack.yaml("environments.yaml")
        self.repositories = pack.yaml("repositories.yaml")
        self.contract_md: str = pack.files["contract.md"].parsed
        self.gates = pack.acceptance_gates()
        self.oracles = pack.oracle_definitions()
        self.index = load_acceptance_index(pack.root)
        self.applicability = ApplicabilityCompiler(pack)
        self.output_keys: list[str] = list(self.contract.get("contract_compiler_outputs", []))

    # ------------------------------------------------------------------

    def compile(self) -> CompiledProject:
        self._check_contract_version()
        compiled = CompiledProject(
            project_id=self.pack.project_id,
            contract_id=self.pack.contract_id,
            contract_version=self.pack.contract_version,
            pack_manifest_hash=self.pack.manifest_hash,
            gates=self.gates,
        )
        compiled.outputs = {key: [] for key in self.output_keys}
        graph = compiled.graph
        graph.add_node(
            f"CONTRACT:{self.pack.contract_id}@{self.pack.contract_version}",
            "Contract",
            manifest_hash=self.pack.manifest_hash,
        )

        compiled.catalog = build_catalog(self.pack)
        compiled.findings.extend(compiled.catalog.findings)

        compiled.plan_validation = validate_against_pack(self.pack)
        self._record_plan_findings(compiled)

        self._compile_requirements(compiled)
        self._compile_acceptance_criteria(compiled)
        self._compile_phases(compiled)
        self._compile_work_breakdown(compiled)
        self._compile_methodologies(compiled)
        self._compile_roles(compiled)
        self._compile_model_capabilities(compiled)
        self._compile_artifact_schemas(compiled)
        self._compile_paths(compiled)
        self._compile_source_and_evidence_rules(compiled)
        self._compile_test_obligations(compiled)
        self._compile_oracle_routes(compiled)
        self._compile_success_and_failure(compiled)
        self._compile_review_triggers(compiled)
        self._compile_auto_merge(compiled)
        self._compile_escalation(compiled)
        self._compile_completion(compiled)
        self._compile_amendment(compiled)
        self._compile_dependencies(compiled)

        self._finalise(compiled)
        return compiled

    # ------------------------------------------------------------------
    # helpers

    def _check_contract_version(self) -> None:
        declared = self.pack.contract_version
        if declared != CONTRACT_VERSION:
            raise CompilationError(
                f"pack declares contract version {declared!r}; the governing version is {CONTRACT_VERSION!r} "
                f"(v1.0 + AMENDMENT-001). Compiling against a stale version is {DriftFinding.STALE_CONTRACT_VERSION}."
            )

    def _emit(self, compiled: CompiledProject, output: str, schema_id: str, body: dict[str, Any]) -> CompiledObject:
        if output not in compiled.outputs:
            raise CompilationError(f"{output!r} is not one of the contract's compiler outputs {self.output_keys}")
        obj = emit(schema_id, body)
        compiled.outputs[output].append(obj)
        return obj

    def _gate_file(self, gate_id: str) -> str:
        for row in self.index.get("coverage", []):
            if isinstance(row, dict) and str(row.get("gate", "")).startswith(gate_id):
                return str(row["gate"])
        return ""

    def _gate_assertion_hash(self, gate_id: str) -> str:
        name = self._gate_file(gate_id)
        path = self.pack.root / "acceptance" / "visible" / name
        return content_hash(path.read_bytes()) if name and path.is_file() else ""

    def _record_plan_findings(self, compiled: CompiledProject) -> None:
        validation = compiled.plan_validation
        assert validation is not None
        if validation.missing_from_table:
            compiled.findings.append(
                CompilationFinding(
                    kind="PLAN_ITEM_NOT_COMPILED",
                    detail=f"three_day_plan items absent from the compiler's structure table: {validation.missing_from_table}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )
        if validation.extra_in_table:
            compiled.findings.append(
                CompilationFinding(
                    kind="COMPILED_TASK_NOT_IN_CONTRACT",
                    detail=f"compiler emits tasks the contract's plan does not list: {validation.extra_in_table}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                )
            )
        if validation.unknown_checks:
            compiled.findings.append(
                CompilationFinding(
                    kind="UNKNOWN_ACCEPTANCE_CHECK_REFERENCE",
                    detail=f"plan table references checks the contract does not declare: {validation.unknown_checks}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )
        if validation.checks_without_plan_item:
            compiled.findings.append(
                CompilationFinding(
                    kind="ACCEPTANCE_CHECK_WITHOUT_BUILD_TASK",
                    detail=(
                        "these acceptance checks are gated but no item of the contract's own three_day_plan "
                        f"builds them; their gate tasks depend on the walking skeleton instead: "
                        f"{validation.checks_without_plan_item}"
                    ),
                    severity="observation",
                )
            )

    # ------------------------------------------------------------------
    # output 1 + 2: requirements and acceptance criteria

    def _compile_requirements(self, compiled: CompiledProject) -> None:
        contract_node = f"CONTRACT:{self.pack.contract_id}@{self.pack.contract_version}"
        for requirement in compiled.catalog.requirements:
            body = requirement.as_body()
            body["contract_id"] = self.pack.contract_id
            self._emit(compiled, "requirements", "efah.requirement", body)
            compiled.graph.add_node(
                requirement.requirement_id,
                "Requirement",
                requirement_kind=requirement.kind,
                risk_class=requirement.risk_class,
                blocking=requirement.blocking,
            )
            compiled.graph.add_edge(
                requirement.requirement_id,
                contract_node,
                "derived_from",
                dependency_class="requirement",
                rationale=requirement.source,
            )

        for gate_id in sorted(self.gates):
            compiled.graph.add_node(gate_id, "Gate", blocking=bool(self.gates[gate_id].get("blocking")))
        for requirement in compiled.catalog.requirements:
            for gate_id in requirement.gate_ids:
                if gate_id in compiled.graph.nodes:
                    compiled.graph.add_edge(
                        requirement.requirement_id,
                        gate_id,
                        "verified_by",
                        dependency_class="evaluation_and_oracle",
                        rationale="acceptance gate for this requirement",
                    )

    def _compile_acceptance_criteria(self, compiled: CompiledProject) -> None:
        for criterion in compiled.catalog.criteria:
            self._emit(compiled, "acceptance_criteria", "efah.acceptance_criterion", criterion.as_body())

    # ------------------------------------------------------------------
    # output 3: phases, transitions, and the LangGraph workflow set

    def _compile_phases(self, compiled: CompiledProject) -> None:
        phases = list(self.contract.get("phase_gates", []))
        names = [p["phase"] for p in phases]
        for position, phase in enumerate(phases):
            name = phase["phase"]
            on_pass = names[position + 1] if position + 1 < len(names) else str(ProjectState.VERIFIED_COMPLETE)
            fail_text = str(phase.get("fail", "")).strip()
            on_fail = "typed_blocker" if "blocker" in fail_text or "blocks" in fail_text else "rework_in_phase"
            body = {
                "phase_id": f"PHASE-{position + 1:02d}",
                "name": name,
                "ordinal": position + 1,
                "material": True,
                "pass_condition": str(phase.get("pass", "")).strip(),
                "fail_condition": fail_text,
                "allowed_transitions": [
                    {"on": "pass", "to": on_pass},
                    {"on": "fail", "to": on_fail, "action": fail_text},
                ],
                "requirement_id": f"REQ-PH-{position + 1:03d}",
                "source": f"contract.yaml#phase_gates[{position}]",
                "contract_ref": "contract.md#8.1",
            }
            self._emit(compiled, "phases_and_transitions", "efah.phase", body)
            compiled.graph.add_node(f"PHASE:{name}", "Phase", ordinal=position + 1)

        self._emit(
            compiled,
            "phases_and_transitions",
            "efah.phase_transition_map",
            {
                "phase_order": names,
                "phase_count": len(names),
                "terminal_success_state": str(ProjectState.VERIFIED_COMPLETE),
                "terminal_states": sorted(str(s) for s in TERMINAL_PROJECT_STATES),
                "contract_review_interval_phases": self.project["project"]["contract_review_interval_phases"],
                "contract_ref": "contract.md#8.1,#6.2",
            },
        )

        for graph_name in self.contract.get("langgraph", {}).get("graphs", []):
            self._emit(
                compiled,
                "phases_and_transitions",
                "efah.workflow_definition",
                {
                    "graph_id": graph_name,
                    "runtime": self.contract["product"]["permanent_runtime"],
                    "checkpoint_references": list(self.contract["langgraph"]["checkpoint_references"]),
                    "fresh_per_invocation_worker_sessions": self.contract["langgraph"][
                        "fresh_per_invocation_worker_sessions"
                    ],
                    "persistent_conversation_memory": self.contract["langgraph"][
                        "persistent_model_conversation_memory_default"
                    ],
                    "interrupt_types": sorted(str(i) for i in OwnerInterrupt),
                    "contract_ref": "contract.md#10.2,#10.4,#10.7",
                },
            )

    # ------------------------------------------------------------------
    # output 4: workstreams, milestones, tasks, work units

    def _compile_work_breakdown(self, compiled: CompiledProject) -> None:
        catalog = compiled.catalog
        graph = compiled.graph
        day_titles = {
            1: "Control-plane spine and walking skeleton",
            2: "Workers, RAG, project automation, and Eval Lab",
            3: "Prove autonomy and close out",
        }

        for day in (1, 2, 3):
            ws_id = f"WS-DAY{day}"
            ms_id = f"MS-DAY{day}"
            graph.add_node(ws_id, "Workstream", day=day)
            graph.add_node(ms_id, "Milestone", day=day)
            self._emit(
                compiled,
                "workstreams_milestones_tasks_work_units",
                "efah.workstream",
                {
                    "workstream_id": ws_id,
                    "day": day,
                    "title": day_titles[day],
                    "source": f"contract.yaml#three_day_plan.day_{day}",
                    "contract_ref": "contract.md#24",
                },
            )
            self._emit(
                compiled,
                "workstreams_milestones_tasks_work_units",
                "efah.milestone",
                {
                    "milestone_id": ms_id,
                    "workstream_id": ws_id,
                    "day": day,
                    "title": day_titles[day],
                    "gate_ids": sorted(g for g, b in self.gates.items() if b.get("day") == day),
                    "contract_ref": "contract.md#24",
                },
            )
            graph.add_edge(ms_id, ws_id, "derived_from", dependency_class="task", rationale="milestone of workstream")

        task_id_by_key: dict[str, str] = {}
        for position, item in enumerate(PLAN_ITEMS, start=1):
            task_id_by_key[item.key] = f"TSK-{position:03d}"

        # --- build tasks ---------------------------------------------------
        for item in PLAN_ITEMS:
            task_id = task_id_by_key[item.key]
            requirement_ids = self._requirements_for_item(catalog, item)
            self._add_task(
                compiled,
                task_id=task_id,
                title=item.title,
                day=item.day,
                phase=item.phase,
                task_class=item.task_class,
                risk=item.risk,
                requirement_ids=requirement_ids,
                gate_ids=[
                    g for check in item.acceptance_checks for g in self._gates_for_check(catalog, check)
                ],
                allowed_paths=list(item.allowed_paths),
                owner_role=item.owner,
                estimate_units=item.estimate_units,
                source=f"contract.yaml#three_day_plan.day_{item.day}:{item.key}"
                if not item.amendment_added
                else "AMENDMENT-001#priority",
                origin="amendment" if item.amendment_added else "three_day_plan",
                plan_key=item.key,
                contract_ref=item.contract_ref,
            )

        for item in PLAN_ITEMS:
            task_id = task_id_by_key[item.key]
            for prerequisite in item.depends_on:
                self._add_task_dependency(compiled, task_id, task_id_by_key[prerequisite], "contract plan ordering")

        # --- one verification task per gate --------------------------------
        gate_task_id: dict[str, str] = {}
        for position, gate_id in enumerate(sorted(self.gates), start=1):
            gate = self.gates[gate_id]
            task_id = f"TSK-GATE-{position:03d}"
            gate_task_id[gate_id] = task_id
            requirement_id = catalog.by_gate.get(gate_id)
            requirement_ids = [requirement_id] if requirement_id else []
            day = int(gate.get("day", 3))
            phase = {
                "GATE-D3-25": "release_merge",
                "GATE-D3-26": "closeout_learning",
            }.get(gate_id, "independent_evaluation")
            self._add_task(
                compiled,
                task_id=task_id,
                title=f"Execute and evidence {gate_id}: {gate.get('name', '')}",
                day=day,
                phase=phase,
                task_class="evaluation_or_oracle_authoring",
                risk="high" if gate.get("blocking") else "medium",
                requirement_ids=requirement_ids,
                gate_ids=[gate_id],
                allowed_paths=["tests/**", "evidence/**"],
                owner_role="release_verifier" if gate_id == "GATE-D3-25" else "evidence_auditor",
                estimate_units=1,
                source=f"acceptance/visible/{self._gate_file(gate_id)}",
                origin="acceptance_gate",
                plan_key=gate_id,
                contract_ref="contract.md#25",
            )

        walking_skeleton = task_id_by_key["first_walking_skeleton"]
        for gate_id, task_id in gate_task_id.items():
            builders = [
                task_id_by_key[item.key]
                for item in PLAN_ITEMS
                if any(gate_id in self._gates_for_check(catalog, check) for check in item.acceptance_checks)
            ]
            if not builders:
                builders = [walking_skeleton]
            for builder in builders:
                self._add_task_dependency(compiled, task_id, builder, f"{gate_id} verifies this work")
                compiled.graph.add_edge(
                    builder,
                    gate_id,
                    "tested_by",
                    dependency_class="evaluation_and_oracle",
                    rationale=f"{gate_id} is the acceptance gate for this task",
                )

        # The evidence package cannot be produced before the gates it reports.
        final_task = task_id_by_key["final_evidence_package"]
        for gate_id, task_id in gate_task_id.items():
            if gate_id == "GATE-D3-26":
                continue
            self._add_task_dependency(compiled, final_task, task_id, "Section 27 package reports every gate result")

        # --- work units ----------------------------------------------------
        for task_id, task in sorted(compiled.tasks.items()):
            work_unit_id = task_id.replace("TSK-", "WU-")
            body = {
                "work_unit_id": work_unit_id,
                "task_id": task_id,
                "objective": task["title"],
                "requirement_ids": task["requirement_ids"],
                "contract_version": CONTRACT_VERSION,
                "methodology_ids": task["methodology_ids"],
                "inputs": [{"project_pack_manifest_hash": self.pack.manifest_hash}],
                "allowed_paths": task["allowed_paths"],
                "prohibited_paths": self._prohibited_paths(),
                "required_artifacts": ["evidence_bundle", "test_result_artifact", "trace_span"],
                "success_conditions": self._success_conditions(task),
                "failure_conditions": list(WORK_UNIT_FAILURE_CONDITIONS),
                "next_permitted_actions": sorted(str(s) for s in WORKER_SUBMITTABLE_STATES),
                "assignment_policy": self.autonomy["assignment_policy"],
                "contract_ref": "contract.md#9.4,#9.5",
            }
            self._emit(compiled, "workstreams_milestones_tasks_work_units", "efah.work_unit", body)
            compiled.graph.add_node(work_unit_id, "WorkUnit", task_id=task_id)
            compiled.graph.add_edge(
                work_unit_id, task_id, "derived_from", dependency_class="task", rationale="work unit of task"
            )

    def _requirements_for_item(self, catalog: RequirementCatalog, item: PlanItem) -> list[str]:
        requirement_ids: list[str] = []
        for check in item.acceptance_checks:
            requirement_id = catalog.by_check.get(check)
            if requirement_id:
                requirement_ids.append(requirement_id)
        for kind in item.requirement_kinds:
            requirement_ids.extend(catalog.ids_of_kind(kind))
        # Every task belongs to a Section 8.1 phase, and the phase requirement is
        # the one it can never not be serving.
        for requirement in catalog.requirements:
            if requirement.kind == "phase" and requirement.contract_refs and requirement.contract_refs[0] == item.phase:
                requirement_ids.append(requirement.requirement_id)
        return sorted(set(requirement_ids))

    def _gates_for_check(self, catalog: RequirementCatalog, check: str) -> list[str]:
        requirement_id = catalog.by_check.get(check)
        if not requirement_id:
            return []
        return list(catalog.get(requirement_id).gate_ids)

    def _add_task(
        self,
        compiled: CompiledProject,
        *,
        task_id: str,
        title: str,
        day: int,
        phase: str,
        task_class: str,
        risk: str,
        requirement_ids: list[str],
        gate_ids: list[str],
        allowed_paths: list[str],
        owner_role: str,
        estimate_units: int,
        source: str,
        origin: str,
        plan_key: str,
        contract_ref: str,
    ) -> None:
        selection = self.applicability.select(task_id=task_id, task_class=task_class, risk=risk)
        compiled.methodology_selections[task_id] = selection
        gate_ids = sorted(set(gate_ids))
        requirement_ids = sorted(set(requirement_ids))
        body = {
            "task_id": task_id,
            "title": title,
            "day": day,
            "workstream_id": f"WS-DAY{day}",
            "milestone_id": f"MS-DAY{day}",
            "phase": phase,
            "task_class": task_class,
            "risk_class": risk,
            "state": str(TaskState.PROPOSED),
            "requirement_ids": requirement_ids,
            "gate_ids": gate_ids,
            "methodology_ids": selection.methodology_ids,
            "methodology_source": selection.methodology_source,
            "allowed_paths": allowed_paths,
            "prohibited_paths": self._prohibited_paths(),
            "owner_role": owner_role,
            "estimate_units": estimate_units,
            "origin": origin,
            "plan_key": plan_key,
            "source": source,
            "contract_ref": contract_ref,
            "contract_version": CONTRACT_VERSION,
        }
        self._emit(compiled, "workstreams_milestones_tasks_work_units", "efah.task", body)
        compiled.tasks[task_id] = dict(body)
        compiled.graph.add_node(
            task_id,
            "Task",
            day=day,
            phase=phase,
            estimate_units=estimate_units,
            task_class=task_class,
            risk_class=risk,
        )
        compiled.graph.add_edge(
            task_id, f"MS-DAY{day}", "derived_from", dependency_class="task", rationale="task of milestone"
        )
        if not requirement_ids:
            compiled.findings.append(
                CompilationFinding(
                    kind=str(DriftFinding.UNLINKED_TASK),
                    detail=f"task {task_id} ({title}) links to no requirement",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=task_id,
                )
            )
        for requirement_id in requirement_ids:
            compiled.graph.add_edge(
                requirement_id,
                task_id,
                "implemented_by",
                dependency_class="requirement",
                rationale="compiled requirement-to-task link",
            )

    def _add_task_dependency(self, compiled: CompiledProject, dependent: str, prerequisite: str, why: str) -> None:
        compiled.graph.add_edge(dependent, prerequisite, "depends_on", dependency_class="task", rationale=why)
        compiled.graph.add_edge(prerequisite, dependent, "blocks", dependency_class="task", rationale=why)

    def _prohibited_paths(self) -> list[str]:
        prohibited = [
            f"{repo['name']}/**" for repo in self.repositories.get("sealed_repos", []) if repo.get("name")
        ]
        prohibited.append("project-pack/acceptance/visible/**")  # Section 14.3 hashed assertions
        prohibited.append(".git/**")
        return sorted(prohibited)

    def _success_conditions(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = [
            {"type": "command_exit", "command": "python -m pytest tests/ -q", "expected_exit": 0}
        ]
        for gate_id in task["gate_ids"]:
            gate = self.gates.get(gate_id, {})
            conditions.append(
                {
                    "type": "hidden_holdout" if gate.get("day") == 3 else "integration_path",
                    "gate_id": gate_id,
                    "oracle_type": gate.get("oracle_type"),
                    "expected": "PASS",
                    "assertion_hash": self._gate_assertion_hash(gate_id),
                }
            )
        if task["risk_class"] == "high":
            conditions.append({"type": "mutation_gate", "mutant_set": "implementation_mutants", "required_kill_rate": 1.0})
        return conditions

    # ------------------------------------------------------------------
    # output 5: methodologies by task and risk

    def _compile_methodologies(self, compiled: CompiledProject) -> None:
        self._emit(
            compiled,
            "methodologies_by_task_and_risk",
            "efah.methodology_catalog",
            self.applicability.catalog_body(),
        )
        for task_id, selection in sorted(compiled.methodology_selections.items()):
            body = selection.as_body()
            body["contract_ref"] = "contract.md#13.3"
            self._emit(compiled, "methodologies_by_task_and_risk", "efah.methodology_assignment", body)
            if not selection.required:
                compiled.findings.append(
                    CompilationFinding(
                        kind="TASK_WITHOUT_METHODOLOGY",
                        detail=f"{task_id} selected no required methodology for class {selection.task_class}",
                        severity="blocking",
                        failure_state=ProjectState.FAILED_CONTRACT,
                        subject=task_id,
                    )
                )

    # ------------------------------------------------------------------
    # output 6: role definitions and incompatibility rules

    def _compile_roles(self, compiled: CompiledProject) -> None:
        aliases = self.model_policy["aliases"]
        routing = self.model_policy["gateway_routing"]
        gate_bearing = set(routing["eval"]["permitted_roles"])
        for role in sorted(aliases):
            spec = aliases[role]
            compiled.graph.add_node(f"ROLE:{role}", "Role", family=spec["family"], gateway=spec["gateway"])
            self._emit(
                compiled,
                "role_separation",
                "efah.role_definition",
                {
                    "role": role,
                    "alias": spec["alias"],
                    "family": spec["family"],
                    "gateway": spec["gateway"],
                    "gate_bearing": role in gate_bearing,
                    "tier": spec.get("tier"),
                    "runs_under_identity": spec.get("runs_under_identity", "builder_service_identity"),
                    "blinded_from_other_agents": True,
                    "contract_ref": "contract.md#12.1,#12.3, DEC-002",
                },
            )

        for producer, validator, ref in ROLE_VALIDATION:
            if f"ROLE:{producer}" not in compiled.graph.nodes or f"ROLE:{validator}" not in compiled.graph.nodes:
                continue
            compiled.graph.add_edge(
                f"ROLE:{producer}",
                f"ROLE:{validator}",
                "verified_by",
                dependency_class="task",
                rationale=ref,
            )
            producer_family = aliases[producer]["family"]
            validator_family = aliases[validator]["family"]
            self._emit(
                compiled,
                "role_separation",
                "efah.role_validation_edge",
                {
                    "producer": producer,
                    "validator": validator,
                    "producer_family": producer_family,
                    "validator_family": validator_family,
                    "cross_family": producer_family != validator_family,
                    "contract_ref": ref,
                },
            )
            if producer_family == validator_family:
                compiled.findings.append(
                    CompilationFinding(
                        kind=str(DriftFinding.ROLE_CONFLICT),
                        detail=(
                            f"{validator} validates {producer} from the same family {producer_family!r}; "
                            "Section 12.2 rejects same-family validation where a cross-family alternative exists"
                        ),
                        severity="blocking",
                        failure_state=ProjectState.FAILED_CONTRACT,
                        subject=f"{producer}->{validator}",
                    )
                )

        for rule in self.model_policy.get("role_incompatibilities", []):
            roles = list(rule.get("roles", []))
            if len(roles) != 2:
                continue
            left, right = roles
            for a, b in ((left, right), (right, left)):
                if f"ROLE:{a}" in compiled.graph.nodes and f"ROLE:{b}" in compiled.graph.nodes:
                    compiled.graph.add_edge(
                        f"ROLE:{a}",
                        f"ROLE:{b}",
                        "conflicts_with",
                        dependency_class="task",
                        rationale=str(rule.get("rule", "")),
                    )
            satisfied = self._incompatibility_satisfied(rule, aliases)
            self._emit(
                compiled,
                "role_separation",
                "efah.role_incompatibility",
                {
                    "roles": roles,
                    "rule": rule.get("rule"),
                    "mandatory": str(rule.get("rule", "")).startswith("must"),
                    "satisfied": satisfied,
                    "satisfied_by": rule.get("satisfied_by"),
                    "contract_ref": rule.get("contract_ref", "contract.md#12.2"),
                },
            )
            if not satisfied and str(rule.get("rule", "")).startswith("must"):
                compiled.findings.append(
                    CompilationFinding(
                        kind=str(DriftFinding.ROLE_CONFLICT),
                        detail=f"role incompatibility {roles} with rule {rule.get('rule')} is not satisfied by the alias map",
                        severity="blocking",
                        failure_state=ProjectState.FAILED_CONTRACT,
                        subject=str(roles),
                    )
                )

        self._emit(
            compiled,
            "role_separation",
            "efah.role_separation_policy",
            {
                "rules": list(self.contract["role_separation"]["rules"]),
                "cross_vendor_agreement_is_proof": self.contract["role_separation"]["cross_vendor_agreement_is_proof"],
                "authority_limits": self.model_policy["authority_limits"],
                "blinded_operation": True,
                "contract_ref": "contract.md#12.2,#12.3,#12.5",
            },
        )

    @staticmethod
    def _incompatibility_satisfied(rule: dict[str, Any], aliases: dict[str, Any]) -> bool:
        roles = list(rule.get("roles", []))
        if len(roles) != 2 or any(r not in aliases for r in roles):
            return False
        left, right = (aliases[r] for r in roles)
        text = str(rule.get("rule", ""))
        if "family" in text and left["family"] == right["family"]:
            return False
        return not ("agent" in text and left["alias"] == right["alias"])

    # ------------------------------------------------------------------
    # output 7: model capability requirements

    def _compile_model_capabilities(self, compiled: CompiledProject) -> None:
        aliases = self.model_policy["aliases"]
        routing = self.model_policy["gateway_routing"]
        request_policy = self.model_policy["request_policy"]
        gate_bearing = set(routing["eval"]["permitted_roles"])
        incompatible: dict[str, set[str]] = {}
        for rule in self.model_policy.get("role_incompatibilities", []):
            roles = list(rule.get("roles", []))
            if len(roles) == 2:
                incompatible.setdefault(roles[0], set()).add(roles[1])
                incompatible.setdefault(roles[1], set()).add(roles[0])

        for role in sorted(aliases):
            spec = aliases[role]
            is_gate_bearing = role in gate_bearing
            self._emit(
                compiled,
                "model_capability_requirements",
                "efah.model_capability_requirement",
                {
                    "role": role,
                    "alias": spec["alias"],
                    "required_capabilities": sorted(
                        {"tool_calling", "streaming" if request_policy.get("prefer_streaming") else "non_streaming"}
                    ),
                    "gateway_class": spec["gateway"],
                    "gateway_rationale": routing[spec["gateway"]]["rationale"],
                    "zero_retry_required": is_gate_bearing,
                    "sdk_max_retries": routing["eval"]["client_requirements"]["sdk_max_retries"] if is_gate_bearing else None,
                    "min_max_tokens_for_tool_calls": request_policy["min_max_tokens_for_tool_calls"],
                    "availability_probe_required": self.model_policy["availability_probe"][
                        "required_before_first_dispatch"
                    ],
                    "family_separation_required": bool(incompatible.get(role)),
                    "prohibited_aliases": sorted(
                        aliases[other]["alias"] for other in incompatible.get(role, set()) if other in aliases
                    ),
                    "calibration_required_before_gate_authority": (
                        self.model_policy["judge_calibration"]["required_before_gate_authority"]
                        if role == "judge"
                        else False
                    ),
                    "advisory_only": role == "judge"
                    and self.model_policy["judge_calibration"]["minimum_agreement_to_gate"] is None,
                    "violation_state": routing["violation_state"],
                    "contract_ref": "contract.md#11.1,#17.5, DEC-002",
                },
            )

        self._emit(
            compiled,
            "model_capability_requirements",
            "efah.model_request_policy",
            {
                "global_throttle_max_requests_per_minute": request_policy["global_throttle_max_requests_per_minute"],
                "global_throttle_min_interval_seconds": request_policy["global_throttle_min_interval_seconds"],
                "throttle_scope": request_policy["throttle_scope"],
                "unthrottled_fanout": request_policy["unthrottled_fanout"],
                "prohibited_models": [entry["model"] for entry in self.model_policy.get("prohibited_models", [])],
                "degraded_at_pack_time": list(self.model_policy["degraded_at_pack_time"]["models"]),
                "retry_and_fallback": self.model_policy["retry_and_fallback"],
                "session_policy": self.model_policy["session_policy"],
                "router_factors": list(self.model_policy["router"]["factors"]),
                "contract_ref": "contract.md#11.1,#10.6",
            },
        )

    # ------------------------------------------------------------------
    # output 8: artifact schemas

    def _compile_artifact_schemas(self, compiled: CompiledProject) -> None:
        methodology_policy = self.pack.yaml("methodology-policy.yaml")
        schemas: list[tuple[str, list[str], str]] = [
            (
                "efah.compiled_object_envelope",
                [
                    "schema_id",
                    "schema_version",
                    "contract_id",
                    "contract_version",
                    "methodology_version",
                    "terminus_database",
                    "terminus_branch",
                    "terminus_commit",
                    "content_hash",
                    "created_by_alias",
                    "created_at",
                ],
                "contract.md#8.1",
            ),
            (
                "efah.work_unit",
                [
                    "work_unit_id",
                    "objective",
                    "requirement_ids",
                    "contract_version",
                    "methodology_ids",
                    "inputs",
                    "allowed_paths",
                    "prohibited_paths",
                    "required_artifacts",
                    "success_conditions",
                    "failure_conditions",
                    "next_permitted_actions",
                ],
                "contract.md#9.4",
            ),
            (
                "efah.module_wiring_manifest",
                [
                    "provides",
                    "consumes",
                    "startup_registration",
                    "configuration_schema",
                    "health_check",
                    "integration_test",
                    "e2e_path",
                    "telemetry_span",
                    "dashboard_projection",
                ],
                "contract.md#5.2",
            ),
            (
                "efah.assignment_lease",
                [
                    "assigned_role",
                    "blinded_alias",
                    "ownership_mode",
                    "lease_id",
                    "lease_generation",
                    "lease_expiry",
                    "renewal_policy",
                    "branch_or_worktree",
                    "input_hashes",
                    "permitted_output_schemas",
                ],
                "contract.md#9.5",
            ),
            (
                "efah.context7_snapshot",
                list(self.dependency_policy["context7_snapshot_fields"]),
                "contract.md#16.1",
            ),
            (
                "efah.retrieval_index_row",
                list(self.contract["rag"]["required_index_lineage"]),
                "contract.md#15.3",
            ),
            (
                "efah.dependency_registry_entry",
                list(self.dependency_policy["registry_required_fields"]),
                "contract.md#16.3",
            ),
            (
                "efah.hypothesis",
                list(methodology_policy["hypothesis_discipline"]["required_fields"]),
                "contract.md#7.4",
            ),
            (
                "efah.build_vs_integrate",
                list(methodology_policy["build_vs_integrate_gate"]["record_fields"]),
                "contract.md#14.2",
            ),
            (
                "efah.judge_calibration",
                list(self.model_policy["judge_calibration"]["required_records"]),
                "contract.md#17.5",
            ),
            (
                "efah.graph_checkpoint",
                list(self.contract["langgraph"]["checkpoint_references"]),
                "contract.md#10.4",
            ),
            (
                "efah.trace_correlation",
                markdown.fenced_block(markdown.section(self.contract_md, "23. Observability")),
                "contract.md#23",
            ),
            (
                "efah.evidence_package",
                markdown.fenced_block(markdown.section(self.contract_md, "27. Final Evidence Package")),
                "contract.md#27",
            ),
            (
                "efah.source_assurance",
                markdown.bullets(markdown.section(self.contract_md, "7.3 Source assurance")),
                "contract.md#7.3",
            ),
            (
                "efah.task_event",
                markdown.fenced_block(markdown.section(self.contract_md, "9.2 Task ledger"), index=0),
                "contract.md#9.2",
            ),
            (
                "efah.time_tracking",
                markdown.fenced_block(markdown.section(self.contract_md, "9.8 Time tracking")),
                "contract.md#9.8",
            ),
        ]
        for schema_id, fields_, ref in schemas:
            cleaned = [f.rstrip(";.").strip() for f in fields_ if f.strip()]
            if not cleaned:
                compiled.findings.append(
                    CompilationFinding(
                        kind="ARTIFACT_SCHEMA_EMPTY",
                        detail=f"schema {schema_id} compiled to zero fields from {ref}",
                        severity="blocking",
                        failure_state=ProjectState.FAILED_CONTRACT,
                        subject=schema_id,
                    )
                )
            self._emit(
                compiled,
                "artifact_schemas",
                "efah.artifact_schema",
                {
                    "target_schema_id": schema_id,
                    "schema_version": "1.0",
                    "required_fields": cleaned,
                    "field_count": len(cleaned),
                    "contract_ref": ref,
                },
            )
            compiled.graph.add_node(f"ARTIFACT:{schema_id}", "Artifact", field_count=len(cleaned))

    # ------------------------------------------------------------------
    # output 9: allowed and prohibited repository paths

    def _compile_paths(self, compiled: CompiledProject) -> None:
        layout = markdown.fenced_block(markdown.section(self.contract_md, "5. Repository and Modular-Monolith"))
        roots: set[str] = set()
        modules: set[str] = set()
        current_root = ""
        for line in layout:
            entry = line.strip()
            if not entry.endswith("/"):
                continue
            if not line.startswith(" "):
                current_root = entry
                roots.add(entry.split("/")[0] + "/**")
            elif current_root == "src/" and line.startswith("  ") and not line.startswith("    "):
                modules.add(entry.rstrip("/"))
        self._emit(
            compiled,
            "allowed_and_prohibited_paths",
            "efah.path_policy",
            {
                "scope": "project",
                "allowed_paths": sorted(roots | {"pyproject.toml", ".github/**", "tools/**"}),
                "declared_modules": sorted(modules),
                "prohibited_paths": self._prohibited_paths(),
                "sealed_repository_names": sorted(
                    str(repo["name"])
                    for repo in self.repositories.get("sealed_repos", [])
                    if repo.get("builder_access") == "forbidden"
                ),
                "read_only_paths": ["project-pack/**"],
                "prohibited_reason": {
                    "sealed_repo": "contract.md#17.2 protected verifier isolation",
                    "project-pack/acceptance/visible/**": "contract.md#14.3 hashed assertions",
                },
                "violation_finding": str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                "protected_access_finding": str(DriftFinding.PROTECTED_ASSET_ACCESS),
                "contract_ref": "contract.md#5,#9.4,#17.2",
            },
        )
        for task_id, task in sorted(compiled.tasks.items()):
            self._emit(
                compiled,
                "allowed_and_prohibited_paths",
                "efah.path_policy",
                {
                    "scope": "task",
                    "task_id": task_id,
                    "allowed_paths": task["allowed_paths"],
                    "prohibited_paths": task["prohibited_paths"],
                    "violation_finding": str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                    "contract_ref": "contract.md#9.4",
                },
            )

    # ------------------------------------------------------------------
    # output 10: source and evidence rules

    def _compile_source_and_evidence_rules(self, compiled: CompiledProject) -> None:
        self._emit(
            compiled,
            "source_and_evidence_rules",
            "efah.evidence_rule",
            {
                "rule_id": "EV-TIERS",
                "evidence_tiers_strongest_first": [str(t) for t in EvidenceTier],
                "done_without_named_evidence": "invalid",
                "contract_ref": "contract.md#18",
            },
        )
        self._emit(
            compiled,
            "source_and_evidence_rules",
            "efah.evidence_rule",
            {
                "rule_id": "EV-KNOWLEDGE-TIERS",
                "knowledge_tiers": [str(t) for t in KnowledgeTier],
                "unverified_agent_output_trusted": self.contract["knowledge"]["unverified_agent_output_trusted"],
                "hard_gold_promotion": list(self.contract["knowledge"]["hard_gold_promotion"]),
                "contract_ref": "contract.md#15.5,#15.6",
            },
        )
        self._emit(
            compiled,
            "source_and_evidence_rules",
            "efah.evidence_rule",
            {
                "rule_id": "EV-RESOLVER-ORDER",
                "resolver_order": list(self.contract["resolver_choice"]["order"]),
                "owner_question_rounds_after_intake": self.contract["resolver_choice"][
                    "owner_question_rounds_after_intake"
                ],
                "must_not_ask_about": list(self.autonomy["question_policy"]["must_not_ask_about"]),
                "contract_ref": "contract.md#7.1,#20.2",
            },
        )
        self._emit(
            compiled,
            "source_and_evidence_rules",
            "efah.evidence_rule",
            {
                "rule_id": "EV-SOURCE-ASSURANCE",
                "required_fields": markdown.bullets(markdown.section(self.contract_md, "7.3 Source assurance")),
                "context7_credentials_are_independent_sources": self.contract["context7"][
                    "credentials_are_independent_sources"
                ],
                "contract_ref": "contract.md#7.3,#16",
            },
        )
        self._emit(
            compiled,
            "source_and_evidence_rules",
            "efah.evidence_rule",
            {
                "rule_id": "EV-AUTHORITY-ORDER",
                "authority_order": [
                    "owner_approved_contract_and_amendments",
                    "machine_compiled_requirements_and_gates",
                    "owner_recorded_decisions",
                    "measured_live_state_and_artifacts",
                    "version_pinned_official_documentation",
                    "approved_methodologies",
                    "model_output",
                ],
                "owner_documents": self.pack.owner_documents(),
                "contract_ref": "contract.md#1.2",
            },
        )

        for name, doc_hash in sorted(self.pack.owner_documents().items()):
            compiled.graph.add_node(f"DOC:{name}", "Document", content_hash=doc_hash)
        contract_node = f"CONTRACT:{self.pack.contract_id}@{self.pack.contract_version}"
        for name in sorted(self.pack.owner_documents()):
            compiled.graph.add_edge(
                contract_node,
                f"DOC:{name}",
                "supported_by",
                dependency_class="documentation",
                rationale="owner document in the pack's evidence directory",
            )

    # ------------------------------------------------------------------
    # output 11: visible and hidden test obligations

    def _compile_test_obligations(self, compiled: CompiledProject) -> None:
        for gate_id in sorted(self.gates):
            gate = self.gates[gate_id]
            self._emit(
                compiled,
                "visible_and_hidden_test_obligations",
                "efah.test_obligation",
                {
                    "obligation_id": f"OBL-{gate_id}",
                    "visibility_class": "visible",
                    "gate_id": gate_id,
                    "name": gate.get("name"),
                    "day": gate.get("day"),
                    "blocking": bool(gate.get("blocking")),
                    "oracle_type": gate.get("oracle_type"),
                    "model_judge_in_verdict_path": gate.get("model_judge_in_verdict_path"),
                    "assertion_ids": [a["id"] for a in gate.get("assertions", [])],
                    "assertion_count": len(gate.get("assertions", [])),
                    "evidence_required": list(gate.get("evidence_required", [])),
                    "on_fail": gate.get("on_fail"),
                    "source_file": self._gate_file(gate_id),
                    "assertion_hash": self._gate_assertion_hash(gate_id),
                    "gate_declared_contract_version": str(gate.get("contract_version")),
                    "contract_ref": "contract.md#14.3,#25",
                },
            )

        sealed = {
            repo["name"]: repo
            for repo in self.repositories.get("sealed_repos", [])
            if repo.get("role") == "protected_verifier"
        }
        holder = next(iter(sealed), "protected_verifier")
        hidden_sets = {
            "diagnostic_hidden_tests": "diagnostic",
            "sealed_release_holdouts": "release_holdout",
            "hard_gold": "gold",
            "fresh_challenges": "fresh_challenge",
            "implementation_mutants": "mutant",
            "test_mutants": "mutant",
            "evaluator_mutants": "mutant",
            "workflow_governance_mutants": "mutant",
        }
        for evaluation_set in self.contract["evaluation"]["sets"]:
            visibility = "hidden" if evaluation_set in hidden_sets else "visible"
            self._emit(
                compiled,
                "visible_and_hidden_test_obligations",
                "efah.test_obligation",
                {
                    "obligation_id": f"OBL-SET-{evaluation_set}",
                    "visibility_class": visibility,
                    "evaluation_set": evaluation_set,
                    "class": hidden_sets.get(evaluation_set, "visible_suite"),
                    "held_by": holder if visibility == "hidden" else "build_repository",
                    "implementer_access": "forbidden" if visibility == "hidden" else "read_write",
                    "runtime": self.contract["evaluation"]["runtime"],
                    "contract_ref": "contract.md#17.1,#17.2",
                },
            )

    # ------------------------------------------------------------------
    # output 12: oracle routes

    def _compile_oracle_routes(self, compiled: CompiledProject) -> None:
        hierarchy = list(self.contract["evaluation"]["oracle_hierarchy"])
        for oracle_id in sorted(self.oracles):
            oracle = self.oracles[oracle_id]
            compiled.graph.add_node(oracle_id, "Oracle", level=oracle.get("hierarchy_level"))
            self._emit(
                compiled,
                "oracle_routes",
                "efah.oracle",
                {
                    "oracle_id": oracle_id,
                    "name": oracle.get("name"),
                    "oracle_version": oracle.get("oracle_version"),
                    "hierarchy_level": oracle.get("hierarchy_level"),
                    "deterministic_verdict_path": oracle.get("deterministic_verdict_path"),
                    "model_call_in_verdict_path": oracle.get("model_call_in_verdict_path"),
                    "judge_participates": oracle.get("judge_participates"),
                    "verdict_values": list(oracle.get("verdict_values", [])),
                    "pinned_checker_test_suite": oracle.get("pinned_checker_test_suite"),
                    "known_bad_fixture_count": len(oracle.get("fixtures", {}).get("known_bad", [])),
                    "gaming_probe_count": len(oracle.get("gaming_probes", [])),
                    "contract_ref": "contract.md#17.3,#17.4",
                },
            )

        unbound = 0
        for gate_id in sorted(self.gates):
            gate = self.gates[gate_id]
            oracle_type = str(gate.get("oracle_type", ""))
            level = hierarchy.index(oracle_type) + 1 if oracle_type in hierarchy else None
            candidates = sorted(
                oid for oid, o in self.oracles.items() if o.get("hierarchy_level") == level
            )
            if not candidates:
                unbound += 1
            self._emit(
                compiled,
                "oracle_routes",
                "efah.oracle_route",
                {
                    "route_id": f"ROUTE-{gate_id}",
                    "gate_id": gate_id,
                    "oracle_type": oracle_type,
                    "hierarchy_level": level,
                    "hierarchy": hierarchy,
                    "model_judge_in_verdict_path": bool(gate.get("model_judge_in_verdict_path")),
                    "candidate_oracle_ids": candidates,
                    "uncalibrated_judge_is_gate": self.contract["evaluation"]["uncalibrated_judge_is_gate"],
                    "oracle_health_required": self.contract["evaluation"]["oracle_health_required"],
                    "contract_ref": "contract.md#17.3",
                },
            )
            for oracle_id in candidates:
                compiled.graph.add_edge(
                    gate_id,
                    oracle_id,
                    "evaluated_by",
                    dependency_class="evaluation_and_oracle",
                    rationale=f"oracle hierarchy level {level}",
                )
            if bool(gate.get("model_judge_in_verdict_path")):
                compiled.findings.append(
                    CompilationFinding(
                        kind="JUDGE_IN_DETERMINISTIC_VERDICT_PATH",
                        detail=f"{gate_id} declares a model judge in its verdict path; Section 17.4 forbids it",
                        severity="blocking",
                        failure_state=ProjectState.FAILED_ASSURANCE,
                        subject=gate_id,
                    )
                )
        if unbound:
            compiled.findings.append(
                CompilationFinding(
                    kind="ORACLE_DEFINITION_DECLARES_NO_GATE_BINDING",
                    detail=(
                        f"{unbound} gate(s) route to an oracle hierarchy level with no defined oracle. The three "
                        "ORACLE-*.yaml definitions name no gate_id, so gate-to-oracle binding is by hierarchy "
                        "level only and cannot be checked more tightly from the pack as supplied."
                    ),
                    severity="observation",
                )
            )

    # ------------------------------------------------------------------
    # output 13: success and failure conditions

    def _compile_success_and_failure(self, compiled: CompiledProject) -> None:
        self._emit(
            compiled,
            "success_and_failure_conditions",
            "efah.project_state_policy",
            {
                "terminal_states": sorted(str(s) for s in TERMINAL_PROJECT_STATES),
                "success_state": str(ProjectState.VERIFIED_COMPLETE),
                "not_terminal": list(self.autonomy["terminal_project_states"]["not_terminal"]),
                "task_states_normal": list(self.contract["task_states"]["normal"]),
                "task_states_exceptional": list(self.contract["task_states"]["exceptional"]),
                "worker_terminal_submission": self.contract["task_states"]["worker_terminal_submission"],
                "gate_terminal_pass": self.contract["task_states"]["gate_terminal_pass"],
                "all_task_states": sorted(str(s) for s in TaskState),
                "failure_classes": sorted(str(f) for f in FailureClass),
                "contract_ref": "contract.md#6.2,#9.3,#10.6",
            },
        )
        for task_id, task in sorted(compiled.tasks.items()):
            self._emit(
                compiled,
                "success_and_failure_conditions",
                "efah.success_condition",
                {
                    "task_id": task_id,
                    "work_unit_id": task_id.replace("TSK-", "WU-"),
                    "success_conditions": self._success_conditions(task),
                    "failure_conditions": list(WORK_UNIT_FAILURE_CONDITIONS),
                    "on_exhaustion": self.model_policy["retry_and_fallback"]["on_exhaustion"],
                    "max_retries_per_work_unit": self.model_policy["retry_and_fallback"]["max_retries_per_work_unit"],
                    "contract_ref": "contract.md#9.4,#10.6",
                },
            )

    # ------------------------------------------------------------------
    # output 14: contract re-review triggers

    def _compile_review_triggers(self, compiled: CompiledProject) -> None:
        review = self.contract["contract_review"]
        interval = self.project["project"]["contract_review_interval_phases"]
        self._emit(
            compiled,
            "contract_review_triggers",
            "efah.contract_review_trigger",
            {
                "trigger_id": "CRT-INTERVAL",
                "trigger_type": "periodic",
                "interval_material_phases": interval,
                "default_if_omitted": review["default_interval_material_phases"],
                "source": "project.yaml#project.contract_review_interval_phases",
                "outcomes": list(review["outcomes"]),
                "advances_automatically_only_on": "CONTRACT_REAFFIRMED",
                "contract_ref": "contract.md#19.3,#19.4",
            },
        )
        for position, event in enumerate(review["event_triggers"], start=1):
            self._emit(
                compiled,
                "contract_review_triggers",
                "efah.contract_review_trigger",
                {
                    "trigger_id": f"CRT-EV-{position:02d}",
                    "trigger_type": "event",
                    "event": event,
                    "source": f"contract.yaml#contract_review.event_triggers[{position - 1}]",
                    "outcomes": list(review["outcomes"]),
                    "advances_automatically_only_on": "CONTRACT_REAFFIRMED",
                    "contract_ref": "contract.md#19.3",
                },
            )

    # ------------------------------------------------------------------
    # output 15: auto-merge conditions

    def _compile_auto_merge(self, compiled: CompiledProject) -> None:
        requirements = self.contract["auto_merge_requirements"]
        policy_requirements = self.autonomy["auto_merge_requirements"]
        for position, (name, expected) in enumerate(sorted(requirements.items()), start=1):
            in_policy = policy_requirements.get(name)
            self._emit(
                compiled,
                "auto_merge_conditions",
                "efah.auto_merge_condition",
                {
                    "condition_id": f"AM-{position:02d}",
                    "name": name,
                    "expected": expected,
                    "declared_in_autonomy_policy": in_policy,
                    "agrees_with_policy": in_policy == expected,
                    "requirement_id": f"REQ-AM-{position:03d}",
                    "contract_ref": "contract.md#21.2",
                },
            )
            if in_policy != expected:
                compiled.findings.append(
                    CompilationFinding(
                        kind=str(DriftFinding.REQUIREMENT_WEAKENING),
                        detail=(
                            f"auto-merge condition {name}: contract requires {expected!r}, "
                            f"autonomy-policy.yaml declares {in_policy!r}"
                        ),
                        severity="blocking",
                        failure_state=ProjectState.FAILED_CONTRACT,
                        subject=name,
                    )
                )
        self._emit(
            compiled,
            "auto_merge_conditions",
            "efah.merge_authority",
            {
                "performed_by": self.autonomy["merge_authority"]["performed_by"],
                "implementing_agent_may_self_certify": self.autonomy["merge_authority"][
                    "implementing_agent_may_self_certify"
                ],
                "green_pr_waits_for_human_message": self.autonomy["merge_authority"][
                    "green_pr_waits_for_human_message"
                ],
                "gate_sequence": markdown.arrow_steps(
                    markdown.fenced_block(markdown.section(self.contract_md, "21.1 Required gate sequence"))
                ),
                "contract_ref": "contract.md#21.1,#21.2",
            },
        )

    # ------------------------------------------------------------------
    # output 16: human escalation conditions

    def _compile_escalation(self, compiled: CompiledProject) -> None:
        declared = list(self.autonomy["human_interrupts_only"])
        typed = sorted(str(i) for i in OwnerInterrupt)
        if sorted(declared) != typed:
            compiled.findings.append(
                CompilationFinding(
                    kind=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                    detail=f"autonomy-policy human_interrupts_only {sorted(declared)} != contract Section 10.7 {typed}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )
        for position, interrupt in enumerate(declared, start=1):
            self._emit(
                compiled,
                "human_escalation_conditions",
                "efah.escalation_condition",
                {
                    "condition_id": f"ESC-{position:02d}",
                    "interrupt_type": interrupt,
                    "closed_list": True,
                    "source": "contract.yaml#autonomy.human_interrupts_only",
                    "contract_ref": "contract.md#10.7",
                },
            )
        question_policy = self.autonomy["question_policy"]
        self._emit(
            compiled,
            "human_escalation_conditions",
            "efah.escalation_policy",
            {
                "must_not_interrupt_for": list(self.autonomy["must_not_interrupt_for"]),
                "max_initial_owner_question_rounds": question_policy["max_initial_owner_question_rounds"],
                "batched": question_policy["batched"],
                "required_question_fields": list(question_policy["required_question_fields"]),
                "delivery_channel": question_policy["delivery_channel"],
                "owner_response_sla_hours": question_policy["owner_response_sla_hours"],
                "on_timeout": question_policy["on_timeout"],
                "blocked_task_state": str(TaskState.BLOCKED_OWNER_DECISION),
                "contract_ref": "contract.md#20.2,#20.3",
            },
        )

    # ------------------------------------------------------------------
    # output 17: completion conditions

    def _compile_completion(self, compiled: CompiledProject) -> None:
        package_fields = markdown.fenced_block(markdown.section(self.contract_md, "27. Final Evidence Package"))
        self._emit(
            compiled,
            "completion_conditions",
            "efah.completion_condition",
            {
                "condition_id": "CC-EVIDENCE-PACKAGE",
                "required_state": str(ProjectState.VERIFIED_COMPLETE),
                "evidence_package_fields": package_fields,
                "field_count": len(package_fields),
                "prose_summary_alone_is_completion": False,
                "contract_ref": "contract.md#27",
            },
        )
        self._emit(
            compiled,
            "completion_conditions",
            "efah.completion_condition",
            {
                "condition_id": "CC-ACCEPTANCE-CHECKS",
                "required_state": str(ProjectState.VERIFIED_COMPLETE),
                "acceptance_checks": list(self.contract["acceptance_checks"]),
                "acceptance_check_count": len(self.contract["acceptance_checks"]),
                "all_must_be_demonstrated_with_artifacts_and_traces": True,
                "gate_ids": sorted(self.gates),
                "blocking_gate_ids": sorted(g for g, b in self.gates.items() if b.get("blocking")),
                "contract_ref": "contract.md#25",
            },
        )
        self._emit(
            compiled,
            "completion_conditions",
            "efah.completion_condition",
            {
                "condition_id": "CC-DELIVERY-PRIORITY",
                "delivery_priority": list(self.project.get("delivery_priority", [])),
                "silent_reordering": self.project.get("silent_reordering"),
                "deadline_posture": self.project.get("deadline_posture", {}),
                "superseded_by": "DEC-005",
                "contract_ref": "project.yaml#delivery_priority, DEC-005",
            },
        )
        self._emit(
            compiled,
            "completion_conditions",
            "efah.completion_condition",
            {
                "condition_id": "CC-NON-GOALS",
                "non_goals": list(self.contract["product"]["non_goals"]),
                "deadline_non_goals": markdown.bullets(
                    markdown.section(self.contract_md, "28. Explicit Non-Goals")
                ),
                "honest_debt_required": True,
                "contract_ref": "contract.md#28",
            },
        )

    # ------------------------------------------------------------------
    # AMENDMENT-001 recompilation (Section 1.3 steps 6 and 7) and DEC-005

    def _compile_amendment(self, compiled: CompiledProject) -> None:
        owner_docs = self.pack.owner_documents()
        recompilation = recompile(
            pack_root=self.pack.root,
            contract_md=self.contract_md,
            gates=self.gates,
            project_yaml_text=self.pack.files["project.yaml"].path.read_text(),
            decisions_dir=self.repo_root / "docs" / "decisions",
            document_hash=owner_docs.get("AMENDMENT-001-owner-control-surface.md", ""),
        )
        compiled.recompilation = recompilation

        contract_node = f"CONTRACT:{self.pack.contract_id}@{self.pack.contract_version}"
        compiled.graph.add_node("AMENDMENT-001", "Amendment", approved_at=recompilation.approved_at)
        compiled.graph.add_node(f"CONTRACT:{self.pack.contract_id}@1.0", "Contract", superseded=True)
        compiled.graph.add_edge(
            contract_node,
            f"CONTRACT:{self.pack.contract_id}@1.0",
            "supersedes",
            dependency_class="documentation",
            rationale="v1.1 = v1.0 + AMENDMENT-001, owner-approved 2026-08-02",
        )
        compiled.graph.add_edge(
            "AMENDMENT-001",
            contract_node,
            "derived_from",
            dependency_class="documentation",
            rationale="amendment of the governing contract",
        )

        self._emit(
            compiled,
            "phases_and_transitions",
            "efah.amendment_recompilation",
            recompilation.step_6_body(),
        )
        self._emit(
            compiled,
            "requirements",
            "efah.amendment_revalidation",
            recompilation.step_7_body(),
        )
        for record in recompilation.revalidation_records:
            node_id = f"OBJ:{record.object_ref}"
            compiled.graph.add_node(node_id, "ContractObject", name=record.object_name, changed=record.changed)
            compiled.graph.add_edge(
                node_id,
                "AMENDMENT-001",
                "invalidated_by" if record.changed else "supported_by",
                dependency_class="requirement",
                rationale=record.reason,
            )
            self._emit(compiled, "requirements", "efah.revalidation_record", record.as_body())

        self._emit(
            compiled,
            "completion_conditions",
            "efah.delivery_priority",
            recompilation.delivery_priority.as_body(),
        )
        compiled.graph.add_node("DEC-005", "Decision", interrupt_class=str(OwnerInterrupt.OWNER_PRIORITY_DECISION))
        compiled.graph.add_node("PROJECT-PRIORITY", "Decision", source="project.yaml#delivery_priority")
        compiled.graph.add_edge(
            "DEC-005",
            "PROJECT-PRIORITY",
            "supersedes",
            dependency_class="documentation",
            rationale="owner reordered GATE-D1-10 ahead of GATE-D1-07; both remain blocking",
        )

    # ------------------------------------------------------------------
    # remaining Section 9.6 dependency classes, then the manifest

    def _compile_dependencies(self, compiled: CompiledProject) -> None:
        graph = compiled.graph
        stack = self.dependency_policy["selected_stack"]
        language_node = "PKG:python"
        graph.add_node(language_node, "SoftwarePackage", version=str(stack["language"]["version"]))
        for key, spec in sorted(stack.items()):
            if key == "language":
                continue
            node_id = f"PKG:{spec['component']}"
            graph.add_node(node_id, "SoftwarePackage", version=str(spec.get("version")), replaceable=spec.get("replaceable"))
            graph.add_edge(
                node_id,
                language_node,
                "compatible_with",
                dependency_class="software_package",
                rationale=f"runs on python {stack['language']['version']}",
            )
        for prohibited in self.dependency_policy.get("prohibited", []):
            node_id = f"PKG:{prohibited['component']}"
            graph.add_node(node_id, "SoftwarePackage", prohibited=True)
            for key, spec in stack.items():
                if key == "language":
                    continue
                if spec["component"] in str(prohibited.get("reason", "")):
                    graph.add_edge(
                        node_id,
                        f"PKG:{spec['component']}",
                        "conflicts_with",
                        dependency_class="software_package",
                        rationale=str(prohibited.get("reason")),
                    )

        dev = self.environments["environments"]["dev"]
        for service in sorted(k for k, v in dev.items() if isinstance(v, dict)):
            graph.add_node(f"SVC:{service}", "Service", environment="dev")
        graph.add_node("ENV:dev", "Environment", default=self.environments.get("default_environment") == "dev")
        graph.add_node("RC:release-candidate", "ReleaseCandidate", contract_version=CONTRACT_VERSION)
        graph.add_edge(
            "RC:release-candidate",
            "ENV:dev",
            "deployed_to",
            dependency_class="deployment_environment",
            rationale="Section 22 risk-selected rollout target for the deadline build",
        )
        graph.add_node("GOLD:candidate-001", "GoldCandidate", tier=str(KnowledgeTier.T7_HARD_GOLD))

        task_by_key = {task["plan_key"]: task_id for task_id, task in compiled.tasks.items()}
        for plan_key, packages in PLAN_ITEM_PACKAGES.items():
            task_id = task_by_key.get(plan_key)
            if task_id is None:
                continue
            for package_key in packages:
                spec = stack.get(package_key)
                if not spec:
                    continue
                graph.add_edge(
                    task_id,
                    f"PKG:{spec['component']}",
                    "depends_on",
                    dependency_class="software_package",
                    rationale="Section 14.2 integrate, do not rebuild",
                )
        for plan_key, services in PLAN_ITEM_SERVICES.items():
            task_id = task_by_key.get(plan_key)
            if task_id is None:
                continue
            for service in services:
                node_id = f"SVC:{service}"
                if node_id in graph.nodes:
                    graph.add_edge(
                        task_id,
                        node_id,
                        "depends_on",
                        dependency_class="service",
                        rationale="live service required by this task",
                    )
        gold_task = task_by_key.get("kedb_and_gold_candidate_promotion")
        if gold_task:
            graph.add_edge(
                "GOLD:candidate-001",
                gold_task,
                "produced_by",
                dependency_class="knowledge_and_gold",
                rationale="Section 15.6 gold promotion",
            )
        evidence_task = task_by_key.get("final_evidence_package")
        if evidence_task:
            graph.add_edge(
                "RC:release-candidate",
                evidence_task,
                "produced_by",
                dependency_class="artifact",
                rationale="Section 27 evidence package binds the release candidate",
            )
            for node_id in graph.nodes_of_kind("Artifact"):
                graph.add_edge(
                    node_id,
                    evidence_task,
                    "produced_by",
                    dependency_class="artifact",
                    rationale="artifact schema realised by the evidence package",
                )

        compiled.cycle_report = graph.cycles()
        compiled.critical_path = graph.critical_path()

        for edge in graph.edges:
            self._emit(compiled, "dependencies_and_critical_path", "efah.dependency", edge.as_body())
        self._emit(
            compiled,
            "dependencies_and_critical_path",
            "efah.critical_path",
            {
                **compiled.critical_path.as_body(),
                "task_titles": [
                    compiled.tasks[n]["title"] for n in compiled.critical_path.nodes if n in compiled.tasks
                ],
                "contract_ref": "contract.md#8,#9.6",
            },
        )
        self._emit(
            compiled,
            "dependencies_and_critical_path",
            "efah.cycle_detection_report",
            {**compiled.cycle_report.as_body(), "contract_ref": "contract.md#8.1"},
        )
        self._emit(
            compiled,
            "dependencies_and_critical_path",
            "efah.dependency_map",
            {
                "edge_types": sorted(graph.edge_type_counts()),
                "edges_by_type": graph.edge_type_counts(),
                "dependency_classes": sorted({e.dependency_class for e in graph.edges}),
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
                "contract_ref": "contract.md#9.6",
            },
        )

    # ------------------------------------------------------------------

    def _finalise(self, compiled: CompiledProject) -> None:
        graph = compiled.graph
        unlinked = graph.unlinked_tasks()
        for task_id in unlinked:
            compiled.findings.append(
                CompilationFinding(
                    kind=str(DriftFinding.UNLINKED_TASK),
                    detail=f"task {task_id} has no implemented_by edge from a requirement",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=task_id,
                )
            )
        for cycle in compiled.cycle_report.cycles:
            compiled.findings.append(
                CompilationFinding(
                    kind="CIRCULAR_DEPENDENCY",
                    detail=f"cycle detected: {' -> '.join(cycle)}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )
        if compiled.critical_path.length == 0:
            compiled.findings.append(
                CompilationFinding(
                    kind="EMPTY_CRITICAL_PATH",
                    detail="no critical path could be computed from the task dependency graph",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )

        missing_methodology = [
            task_id
            for task_id, task in compiled.tasks.items()
            if not task["methodology_ids"] or task["methodology_source"] != "applicability_compiler"
        ]
        for task_id in missing_methodology:
            compiled.findings.append(
                CompilationFinding(
                    kind="METHODOLOGY_PROVENANCE",
                    detail=f"{task_id} does not carry compiler-selected methodologies",
                    severity="blocking",
                    # GATE-D1-03 A5 fails to TaskState.FAILED_SCOPE at the task
                    # level; at the project level that is FAILED_CONTRACT.
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=f"{task_id}:{TaskState.FAILED_SCOPE}",
                )
            )

        section_8 = {}
        for bullet, keys in SECTION_8_OUTPUTS:
            counts = {key: len(compiled.outputs.get(key, [])) for key in keys}
            section_8[bullet] = {
                "contract_yaml_keys": list(keys),
                "object_counts": counts,
                "present": all(count > 0 for count in counts.values()),
            }
        missing = [name for name, info in section_8.items() if not info["present"]]
        empty_keys = [key for key in self.output_keys if not compiled.outputs.get(key)]
        if missing or empty_keys:
            compiled.findings.append(
                CompilationFinding(
                    kind="MISSING_COMPILER_OUTPUT",
                    detail=f"Section 8 outputs not produced: {missing}; empty contract.yaml keys: {empty_keys}",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                )
            )
        if len(self.output_keys) != len(SECTION_8_OUTPUTS):
            compiled.findings.append(
                CompilationFinding(
                    kind="OUTPUT_LIST_ARITY",
                    detail=(
                        f"contract.yaml lists {len(self.output_keys)} contract_compiler_outputs while contract.md "
                        f"Section 8 states {len(SECTION_8_OUTPUTS)} obligations. The difference is the first "
                        "Section 8 bullet, 'requirement IDs and acceptance criteria', which the YAML splits into "
                        "'requirements' and 'acceptance_criteria'. GATE-D1-03 A1 counts seventeen; both views are "
                        "reported in the manifest and every YAML key is produced."
                    ),
                    severity="observation",
                )
            )

        manifest_body = {
            "project_id": compiled.project_id,
            "contract_id": compiled.contract_id,
            "contract_version": compiled.contract_version,
            "pack_manifest_hash": compiled.pack_manifest_hash,
            "contract_yaml_output_keys": self.output_keys,
            "contract_yaml_output_count": len(self.output_keys),
            "section_8_output_count": len(SECTION_8_OUTPUTS),
            "section_8_outputs": section_8,
            "all_seventeen_present": not missing,
            "object_counts": compiled.counts(),
            "total_compiled_objects": sum(len(v) for v in compiled.outputs.values()) + 1,
            "unlinked_tasks": unlinked,
            "cycles": compiled.cycle_report.cycles,
            "critical_path_length": compiled.critical_path.length,
            "methodology_source": "applicability_compiler",
            "tasks_without_compiler_methodology": missing_methodology,
            "blocking_findings": [f.as_body() for f in compiled.blocking_findings],
            "observations": [f.as_body() for f in compiled.observations],
            "contract_ref": "contract.md#8, GATE-D1-03",
        }
        compiled.manifest = emit("efah.compiler_output_manifest", manifest_body)


def compile_pack(pack: ProjectPack, *, repo_root: Path | None = None) -> CompiledProject:
    """Convenience entry point used by the CLI and the tests."""
    return ContractCompiler(pack, repo_root=repo_root).compile()
