"""The compiler's structural reading of the contract's three-day plan.

Contract Section 8 requires the compiler to emit "workstreams, milestones,
tasks, and work units". The *names* of the build tasks are not invented here --
they are exactly ``contract.yaml -> three_day_plan.day_1/2/3``, checked at
compile time. What this table adds is the structure the contract states in prose
but not in YAML: which acceptance check each plan item serves (Section 25),
which Section 8.1 phase it belongs to, its task class and risk for the Section
13.3 applicability compiler, its ordering constraints, and its allowed paths
(Section 9.4).

Every entry cites the contract clause it comes from. If an entry cannot be
justified from the contract it does not belong here; and if the contract's plan
changes, :func:`validate_against_pack` fails rather than letting a stale table
be used.

AMENDMENT-001 adds one Day-1 item that v1.0's ``three_day_plan`` does not carry:
the owner control surface (contract v1.1 Section 11.7). It is marked
``amendment_added`` so the recompilation record (Section 1.3 step 6) can show
exactly what the amendment changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from integrations.pack import ProjectPack


@dataclass(frozen=True)
class PlanItem:
    key: str
    day: int
    title: str
    phase: str
    task_class: str
    risk: str
    #: ``contract.yaml -> acceptance_checks`` names this item serves.
    acceptance_checks: tuple[str, ...]
    #: other plan-item keys this item needs first
    depends_on: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    contract_ref: str
    #: requirement kinds this item is responsible for as a whole family
    requirement_kinds: tuple[str, ...] = ()
    estimate_units: int = 1
    amendment_added: bool = False
    owner: str = "implementer"


#: Section 5 greenfield layout. Used to build per-task allowed paths.
_SRC = "src/{}/**"

DAY_1: tuple[PlanItem, ...] = (
    PlanItem(
        key="modular_monolith_structure",
        day=1,
        title="Modular-monolith structure and architecture rules",
        phase="architecture_sdd",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=(),
        depends_on=(),
        allowed_paths=("src/**", "tests/architecture/**", "docs/architecture/**", "pyproject.toml"),
        contract_ref="contract.md#3.1,#5,#5.1",
        requirement_kinds=("non_goal",),
        estimate_units=2,
    ),
    PlanItem(
        key="terminusdb_schemas",
        day=1,
        title="TerminusDB main and protected schemas",
        phase="architecture_sdd",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("project_pack_imports_to_terminusdb_branch", "schemas_validate_and_are_version_bound"),
        depends_on=("modular_monolith_structure",),
        allowed_paths=(_SRC.format("ontology"), _SRC.format("integrations"), "tests/contract/**"),
        contract_ref="contract.md#15.2,#24 Day 1",
        estimate_units=2,
    ),
    PlanItem(
        key="project_task_dependency_ledgers",
        day=1,
        title="Project, task, dependency, assignment, artifact and evaluation ledgers",
        phase="project_compilation",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("contract_compiles_to_project_task_dependency_graph",),
        depends_on=("terminusdb_schemas",),
        allowed_paths=(
            _SRC.format("contracts"),
            _SRC.format("requirements"),
            _SRC.format("tasks"),
            _SRC.format("dependencies"),
            _SRC.format("methodologies"),
            "tests/unit/**",
            "tests/contract/**",
        ),
        contract_ref="contract.md#8,#9.1,#9.6",
        requirement_kinds=("compiler_output", "phase"),
        estimate_units=3,
    ),
    PlanItem(
        key="langgraph_skeletons",
        day=1,
        title="LangGraph project and task graph skeletons with checkpointing",
        phase="architecture_sdd",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("langgraph_resumes_without_restart",),
        depends_on=("project_task_dependency_ledgers",),
        allowed_paths=(_SRC.format("workflows"), "tests/integration/**"),
        contract_ref="contract.md#10.2,#10.3",
        estimate_units=2,
    ),
    PlanItem(
        key="plane_projection",
        day=1,
        title="Plane projection adapter",
        phase="integration_composition",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("plane_project_and_assurance_views",),
        depends_on=("project_task_dependency_ledgers",),
        allowed_paths=(_SRC.format("dashboard"), _SRC.format("integrations")),
        contract_ref="contract.md#4,#11.6",
    ),
    PlanItem(
        key="context7_cache",
        day=1,
        title="Context7 snapshot cache, hashing and dependency link",
        phase="research_and_hypotheses",
        task_class="research_or_debugging",
        risk="medium",
        acceptance_checks=(
            "context7_snapshot_hash_and_dependency_link",
            "dependency_change_impact_and_revalidation",
        ),
        depends_on=("modular_monolith_structure",),
        allowed_paths=(_SRC.format("dependencies"), _SRC.format("research"), _SRC.format("impact")),
        contract_ref="contract.md#16.1,#16.2",
    ),
    PlanItem(
        key="model_router_path",
        day=1,
        title="Deterministic blinded model-router path through LiteLLM",
        phase="architecture_sdd",
        task_class="infrastructure_integration",
        risk="high",
        acceptance_checks=("blinded_model_identity", "fresh_litellm_worker_sessions"),
        depends_on=("modular_monolith_structure",),
        allowed_paths=(_SRC.format("models"), _SRC.format("workers")),
        contract_ref="contract.md#11.1,#11.2,#12.3",
        estimate_units=2,
    ),
    PlanItem(
        key="ci_skeleton",
        day=1,
        title="CI pipeline with executable skeleton gates",
        phase="release_merge",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("green_pr_auto_merged", "mechanical_commit_trace_artifact_binding"),
        depends_on=("modular_monolith_structure",),
        allowed_paths=(".github/**", "tools/**"),
        contract_ref="contract.md#21.1,#24 Day 1",
    ),
    PlanItem(
        key="owner_control_surface",
        day=1,
        title="Vendor-neutral owner control surface (LangGraph-backed FastAPI endpoint)",
        phase="walking_skeleton",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("owner_control_surface_vendor_neutral",),
        depends_on=("langgraph_skeletons", "plane_projection"),
        allowed_paths=(_SRC.format("api"), _SRC.format("dashboard"), _SRC.format("workflows")),
        contract_ref="AMENDMENT-001 / contract v1.1 #11.7",
        estimate_units=2,
        amendment_added=True,
    ),
    PlanItem(
        key="first_walking_skeleton",
        day=1,
        title="First complete walking-skeleton run",
        phase="walking_skeleton",
        task_class="trust_critical_code_change",
        risk="high",
        # Section 14.4 is the skeleton itself; Section 20.2's question round and
        # GATE-D1-07 A2's credential-stripped run both execute through it.
        acceptance_checks=("vendor_neutral_after_deadline", "questions_are_bounded_and_batched"),
        depends_on=(
            "terminusdb_schemas",
            "langgraph_skeletons",
            "plane_projection",
            "model_router_path",
            "ci_skeleton",
            "owner_control_surface",
        ),
        allowed_paths=(_SRC.format("composition"), _SRC.format("cli"), "tests/e2e/**"),
        contract_ref="contract.md#14.4 as amended by AMENDMENT-001",
        estimate_units=3,
    ),
)

DAY_2: tuple[PlanItem, ...] = (
    PlanItem(
        key="fresh_worker_adapters",
        day=2,
        title="Fresh per-invocation worker sessions through LiteLLM",
        phase="visible_convergence",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("fresh_litellm_worker_sessions", "vendor_neutral_after_deadline"),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("workers"),),
        contract_ref="contract.md#10.5,#24 Day 2",
    ),
    PlanItem(
        key="code_intelligence",
        day=2,
        title="Code intelligence: git, ripgrep, tree-sitter, language servers",
        phase="visible_convergence",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("mature_dependency_integration_and_reimplementation_rejection",),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("knowledge"), _SRC.format("integrations")),
        contract_ref="contract.md#4,#14.2",
    ),
    PlanItem(
        key="rag_ingestion_and_retrieval",
        day=2,
        title="Docling/LanceDB evidence ingestion and retrieval with lineage",
        phase="visible_convergence",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("rag_result_resolves_to_terminus_commit", "unverified_knowledge_not_promoted"),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("knowledge"), _SRC.format("evidence")),
        contract_ref="contract.md#15.1,#15.3,#15.4",
        estimate_units=2,
    ),
    PlanItem(
        key="task_leases_and_worktrees",
        day=2,
        title="Task leases, generation fencing and parallel worktrees",
        phase="visible_convergence",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("leases_worktrees_and_stale_worker_rejection",),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("assignments"), _SRC.format("tasks")),
        contract_ref="contract.md#9.5",
    ),
    PlanItem(
        key="inspect_and_protected_verifier",
        day=2,
        title="Inspect AI task and protected verifier interface",
        phase="independent_evaluation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("protected_verifier_isolation",),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("evaluation"), "verifier-interface/**"),
        contract_ref="contract.md#17.2,#14.5",
        estimate_units=2,
    ),
    PlanItem(
        key="visible_hidden_mutation_oracle_lane",
        day=2,
        title="Visible tests, sealed holdout, mutants and one deterministic oracle",
        phase="independent_evaluation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=(
            "visible_hidden_mutant_same_candidate_commit",
            "oracle_health_and_no_judge_in_deterministic_path",
        ),
        depends_on=("inspect_and_protected_verifier",),
        allowed_paths=(_SRC.format("oracles"), _SRC.format("holdouts"), _SRC.format("mutants")),
        contract_ref="contract.md#17.1,#17.3,#17.4",
        estimate_units=2,
        owner="oracle_author",
    ),
    PlanItem(
        key="observability",
        day=2,
        title="OpenTelemetry and Phoenix correlated tracing",
        phase="integration_composition",
        task_class="infrastructure_integration",
        risk="medium",
        acceptance_checks=("mechanical_commit_trace_artifact_binding",),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("observability"),),
        contract_ref="contract.md#23",
    ),
    PlanItem(
        key="scope_drift_and_contract_review",
        day=2,
        title="Scope-drift engine and contract-review workflow",
        phase="contract_revalidation",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("scope_and_security_expansion_blocked", "periodic_and_event_contract_review"),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("drift"), _SRC.format("impact"), _SRC.format("governance")),
        contract_ref="contract.md#19.1,#19.3,#19.5",
        requirement_kinds=("drift_finding",),
        estimate_units=2,
    ),
    PlanItem(
        key="automatic_pr_and_ci_repair",
        day=2,
        title="Automatic PR creation and CI repair path",
        phase="release_merge",
        task_class="routine_repair",
        risk="low",
        acceptance_checks=("green_pr_auto_merged",),
        depends_on=("ci_skeleton", "first_walking_skeleton"),
        allowed_paths=(_SRC.format("provenance"), ".github/**"),
        contract_ref="contract.md#21,#20.3",
        requirement_kinds=("auto_merge",),
    ),
)

DAY_3: tuple[PlanItem, ...] = (
    PlanItem(
        key="representative_project_end_to_end",
        day=3,
        title="Carry one representative project through the whole system",
        phase="integration_composition",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("verified_complete_evidence_package",),
        depends_on=(
            "fresh_worker_adapters",
            "code_intelligence",
            "rag_ingestion_and_retrieval",
            "task_leases_and_worktrees",
            "visible_hidden_mutation_oracle_lane",
            "observability",
            "scope_drift_and_contract_review",
            "automatic_pr_and_ci_repair",
        ),
        allowed_paths=(_SRC.format("projects"), "tests/e2e/**"),
        contract_ref="contract.md#24 Day 3, DEC-004",
        estimate_units=3,
    ),
    PlanItem(
        key="multivendor_roles",
        day=3,
        title="Run multiple fresh cross-vendor roles",
        phase="visible_convergence",
        task_class="infrastructure_integration",
        risk="high",
        acceptance_checks=("blinded_model_identity",),
        depends_on=("fresh_worker_adapters",),
        allowed_paths=(_SRC.format("models"), _SRC.format("workers")),
        contract_ref="contract.md#12.1,#12.3",
    ),
    PlanItem(
        key="wiring_omission_rejection",
        day=3,
        title="Prove a module with unit tests but no wiring fails",
        phase="integration_composition",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("missing_wiring_fails",),
        depends_on=("first_walking_skeleton",),
        allowed_paths=(_SRC.format("composition"), "tests/integration/**"),
        contract_ref="contract.md#5.2, ORACLE-001",
        owner="integration_verifier",
    ),
    PlanItem(
        key="stale_worker_rejection",
        day=3,
        title="Prove a stale worker submission is rejected",
        phase="independent_evaluation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("leases_worktrees_and_stale_worker_rejection",),
        depends_on=("task_leases_and_worktrees",),
        allowed_paths=(_SRC.format("assignments"), "tests/integration/**"),
        contract_ref="contract.md#9.5, ORACLE-002",
    ),
    PlanItem(
        key="scope_drift_rejection",
        day=3,
        title="Prove scope drift and security expansion are rejected",
        phase="contract_revalidation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("scope_and_security_expansion_blocked",),
        depends_on=("scope_drift_and_contract_review",),
        allowed_paths=(_SRC.format("drift"), "tests/unit/**"),
        contract_ref="contract.md#19.2,#19.5",
    ),
    PlanItem(
        key="holdout_isolation",
        day=3,
        title="Prove protected holdout isolation from the implementer",
        phase="independent_evaluation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("protected_verifier_isolation",),
        depends_on=("inspect_and_protected_verifier",),
        allowed_paths=(_SRC.format("holdouts"), "verifier-interface/**"),
        contract_ref="contract.md#17.2",
    ),
    PlanItem(
        key="mutant_rejection",
        day=3,
        title="Prove a known-bad mutant is rejected",
        phase="independent_evaluation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("known_bad_mutant_rejected",),
        depends_on=("visible_hidden_mutation_oracle_lane",),
        allowed_paths=(_SRC.format("mutants"), "tests/mutation/**"),
        contract_ref="contract.md#17.1,#25.19",
        owner="mutant_author",
    ),
    PlanItem(
        key="checkpoint_resume",
        day=3,
        title="Prove restart/resume from checkpoint without project reset",
        phase="deployment_validation",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("langgraph_resumes_without_restart",),
        depends_on=("langgraph_skeletons",),
        allowed_paths=(_SRC.format("workflows"), "tests/integration/**"),
        contract_ref="contract.md#10.3,#10.6",
    ),
    PlanItem(
        key="green_auto_merge",
        day=3,
        title="Prove a green PR is auto-merged without a human message",
        phase="release_merge",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("green_pr_auto_merged",),
        depends_on=("automatic_pr_and_ci_repair",),
        allowed_paths=(".github/**", _SRC.format("provenance")),
        contract_ref="contract.md#21.2",
        owner="release_verifier",
    ),
    PlanItem(
        key="kedb_and_gold_candidate_promotion",
        day=3,
        title="Promote one verified KEDB item and one hard-gold candidate",
        phase="closeout_learning",
        task_class="evaluation_or_oracle_authoring",
        risk="high",
        acceptance_checks=("unverified_knowledge_not_promoted",),
        depends_on=("rag_ingestion_and_retrieval",),
        allowed_paths=(_SRC.format("knowledge"), _SRC.format("gold")),
        contract_ref="contract.md#15.5,#15.6",
    ),
    PlanItem(
        key="final_evidence_package",
        day=3,
        title="Produce the final evidence package and honest debt",
        phase="closeout_learning",
        task_class="trust_critical_code_change",
        risk="high",
        acceptance_checks=("verified_complete_evidence_package",),
        depends_on=(
            "representative_project_end_to_end",
            "multivendor_roles",
            "wiring_omission_rejection",
            "stale_worker_rejection",
            "scope_drift_rejection",
            "holdout_isolation",
            "mutant_rejection",
            "checkpoint_resume",
            "green_auto_merge",
            "kedb_and_gold_candidate_promotion",
        ),
        allowed_paths=(_SRC.format("evidence"), _SRC.format("provenance"), "docs/**"),
        contract_ref="contract.md#27",
        estimate_units=2,
    ),
)

PLAN_ITEMS: tuple[PlanItem, ...] = DAY_1 + DAY_2 + DAY_3


@dataclass
class PlanValidation:
    """Result of checking this table against the contract it claims to read."""

    missing_from_table: dict[int, list[str]] = field(default_factory=dict)
    extra_in_table: dict[int, list[str]] = field(default_factory=dict)
    unknown_checks: list[str] = field(default_factory=list)
    checks_without_plan_item: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_from_table or self.extra_in_table or self.unknown_checks)

    def as_body(self) -> dict[str, Any]:
        return {
            "plan_items_missing_from_compiler_table": {str(k): v for k, v in self.missing_from_table.items()},
            "plan_items_in_table_but_not_in_contract": {str(k): v for k, v in self.extra_in_table.items()},
            "acceptance_checks_referenced_but_not_in_contract": self.unknown_checks,
            "acceptance_checks_with_no_build_task": self.checks_without_plan_item,
            "table_matches_contract": self.ok,
        }


def validate_against_pack(pack: ProjectPack) -> PlanValidation:
    """Fail loudly if the contract's plan and this table have diverged."""
    contract = pack.yaml("contract.yaml")
    plan = contract.get("three_day_plan", {})
    checks = set(contract.get("acceptance_checks", []))
    result = PlanValidation()

    for day in (1, 2, 3):
        declared = list(plan.get(f"day_{day}", []))
        tabled = [item.key for item in PLAN_ITEMS if item.day == day and not item.amendment_added]
        missing = [k for k in declared if k not in tabled]
        extra = [k for k in tabled if k not in declared]
        if missing:
            result.missing_from_table[day] = missing
        if extra:
            result.extra_in_table[day] = extra

    referenced: set[str] = set()
    for item in PLAN_ITEMS:
        for check in item.acceptance_checks:
            referenced.add(check)
            if check not in checks:
                result.unknown_checks.append(f"{item.key}:{check}")
    result.checks_without_plan_item = sorted(checks - referenced)
    return result
