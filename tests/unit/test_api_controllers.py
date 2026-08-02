"""Controller and router purity, plus use-case behaviour (Sections 11.3, 11.5).

> **11.3** The HTTP/API router maps endpoints to controllers only. It MUST NOT
> contain workflow or model-routing decisions.
>
> **11.5** Controllers translate commands into application use cases. They MUST
> NOT contain persistence-specific code, model prompts, or hidden evaluator
> logic.

Both are enforced with an AST scan rather than a code-review convention, because
the failure mode is gradual: one persistence call in a controller is a shortcut,
five is an architecture.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api.adapters.control_plane_memory import InMemoryControlPlane, RecordingRuntime
from api.context import IdentityKind, Principal, RequestContext, Scope
from api.controllers import (
    ContractController,
    DependencyController,
    EvaluationController,
    ProjectController,
    TaskController,
)
from api.controllers.projects import ContractDriftEngine
from api.errors import GateBypassRejected, NotFound, ScopeExpansionRejected, StaleContractVersion
from api.ports import (
    ControlPlaneReadPort,
    ControlPlaneWritePort,
    DriftEnginePort,
    ProjectionPort,
    RuntimePort,
)
from api.state import EvaluationRecord, TaskRecord
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import ContractReviewOutcome, TaskState, Verdict

SRC = Path(__file__).resolve().parents[2] / "src"
CONTROLLERS = SRC / "api" / "controllers"
ROUTER = SRC / "api" / "router.py"

#: Section 11.5 "persistence-specific code": a controller that reaches a driver
#: or speaks a query language has bound itself to a storage choice.
PERSISTENCE_MODULES = {
    "sqlite3",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "terminusdb_client",
    "pymongo",
    "redis",
    "lancedb",
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
}
#: Section 11.5 "model prompts" and "hidden evaluator logic".
MODEL_MODULES = {"litellm", "openai", "anthropic", "langchain", "langgraph", "inspect_ai", "promptfoo"}
QUERY_LANGUAGE_MARKERS = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "WOQL", "triple(")
PROMPT_MARKERS = ("system prompt", "You are a ", "You are an ", "### Instruction")


def module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def controller_files() -> list[Path]:
    return sorted(CONTROLLERS.glob("*.py"))


# ------------------------------------------------------------ 11.5 purity


def test_controllers_contain_no_persistence_specific_code() -> None:
    offenders = [
        f"{path.name}: {sorted(module_imports(path) & PERSISTENCE_MODULES)}"
        for path in controller_files()
        if module_imports(path) & PERSISTENCE_MODULES
    ]
    assert not offenders, offenders


def test_controllers_contain_no_model_or_evaluator_dependency() -> None:
    offenders = [
        f"{path.name}: {sorted(module_imports(path) & MODEL_MODULES)}"
        for path in controller_files()
        if module_imports(path) & MODEL_MODULES
    ]
    assert not offenders, offenders


def executable_source(path: Path) -> str:
    """Source with docstrings and comments removed.

    Scanning raw text would flag the module docstring that *states* the rule
    ("there is no WOQL here") -- a check that punishes documenting the
    constraint is a check people delete.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_controllers_contain_no_query_language_or_prompt_text() -> None:
    offenders: list[str] = []
    for path in controller_files():
        source = executable_source(path)
        for marker in QUERY_LANGUAGE_MARKERS + PROMPT_MARKERS:
            if marker in source:
                offenders.append(f"{path.name}: {marker!r}")
    assert not offenders, offenders


def test_the_purity_scanner_would_actually_catch_a_violation(tmp_path: Path) -> None:
    """A test that cannot fail is not a test."""
    offender = tmp_path / "bad_controller.py"
    offender.write_text(
        '"""A controller that reaches for persistence."""\n'
        "import sqlite3\n\n\n"
        "def get(task_id):\n"
        '    return sqlite3.connect("x").execute("SELECT * FROM tasks")\n'
    )
    assert module_imports(offender) & PERSISTENCE_MODULES == {"sqlite3"}
    assert "SELECT " in executable_source(offender)


# ------------------------------------------------------------ 11.3 purity


def test_the_router_holds_no_workflow_or_routing_decision() -> None:
    """No branching, no loops, no state transitions in a route handler."""
    tree = ast.parse(ROUTER.read_text(), filename=str(ROUTER))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue  # `_ack` is a response envelope helper, not a decision
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.Match)):
                offenders.append(f"{node.name}: control flow ({type(child).__name__})")
    assert not offenders, offenders


def test_the_router_imports_no_persistence_or_model_dependency() -> None:
    imported = module_imports(ROUTER)
    assert not imported & PERSISTENCE_MODULES
    assert not imported & MODEL_MODULES


def test_every_route_handler_calls_exactly_one_controller_use_case() -> None:
    """"Maps endpoints to controllers only" -- one call, not orchestration."""
    tree = ast.parse(ROUTER.read_text(), filename=str(ROUTER))
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        controller_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "controller"
        ]
        if node.name == "health":
            continue  # reports wiring facts; calls no use case
        if len(controller_calls) != 1:
            offenders.append(f"{node.name}: {len(controller_calls)} controller calls")
    assert not offenders, offenders


# ---------------------------------------------------------------- adapters


def test_default_adapters_satisfy_the_declared_ports() -> None:
    """Section 5.1: cross-module operations use declared application interfaces."""
    control_plane = InMemoryControlPlane()
    assert isinstance(control_plane, ControlPlaneReadPort)
    assert isinstance(control_plane, ControlPlaneWritePort)
    assert isinstance(RecordingRuntime(control_plane), RuntimePort)
    assert isinstance(ContractDriftEngine(control_plane), DriftEnginePort)


def test_the_plane_projection_satisfies_the_projection_port() -> None:
    from integrations.plane import PlaneConfig, PlaneProjection

    projection = PlaneProjection(PlaneConfig(workspace="efah", project_id="x"))
    assert isinstance(projection, ProjectionPort)
    assert projection.may_mutate_authoritative_state is False


def test_the_recording_runtime_does_not_claim_to_execute_a_graph() -> None:
    """Honest debt: "not yet wired" must not be reportable as done."""
    assert RecordingRuntime(InMemoryControlPlane()).executes_graph is False


# ------------------------------------------------------------- behaviour


@pytest.fixture
def wired():
    control_plane = InMemoryControlPlane()
    runtime = RecordingRuntime(control_plane)
    drift = ContractDriftEngine(control_plane)
    control_plane.import_project(
        pack_root="project-pack", requested_by="test", correlation_id="c"
    )
    return control_plane, runtime, drift


@pytest.fixture
def context() -> RequestContext:
    return RequestContext(
        correlation_id="corr",
        request_id="req-0123456789",
        principal=Principal(kind=IdentityKind.HUMAN, subject="owner", scopes=frozenset(Scope)),
    )


def test_project_run_records_the_run_and_binds_it_to_provenance(wired, context) -> None:
    control_plane, runtime, drift = wired
    controller = ProjectController(
        reader=control_plane, writer=control_plane, runtime=runtime, drift_engine=drift
    )
    handle = controller.run(project_id="EFAH-001", context=context)
    assert handle.accepted
    snapshot = control_plane.snapshot("EFAH-001")
    assert snapshot.project.current_run_id == handle.run_id
    assert any(edge.source == f"run:{handle.run_id}" for edge in snapshot.provenance)


def test_project_run_on_an_unknown_project_is_not_found(wired, context) -> None:
    control_plane, runtime, drift = wired
    controller = ProjectController(
        reader=control_plane, writer=control_plane, runtime=runtime, drift_engine=drift
    )
    with pytest.raises(NotFound):
        controller.run(project_id="NOPE", context=context)


@pytest.mark.parametrize(
    "instruction",
    [
        "also build a reporting dashboard",
        "please skip the gate for this one",
        "amend the contract to drop the holdout suite",
        "add a new feature for invoicing",
    ],
)
def test_scope_expanding_instructions_are_classified(wired, instruction: str) -> None:
    _, _, drift = wired
    finding = drift.classify_instruction(project_id="EFAH-001", instruction=instruction)
    assert finding is not None
    assert finding.finding_type == "UNAPPROVED_SCOPE_EXPANSION"


@pytest.mark.parametrize(
    "instruction",
    ["retry the failing test", "resume after the dependency lands", "rerun the oracle check"],
)
def test_ordinary_instructions_are_not_classified_as_drift(wired, instruction: str) -> None:
    _, _, drift = wired
    assert drift.classify_instruction(project_id="EFAH-001", instruction=instruction) is None


def test_graph_reports_a_cycle_rather_than_crashing(wired) -> None:
    """GATE-D1-03 wants a circular graph to fail visibly, not to 500."""
    control_plane, _, _ = wired
    control_plane.upsert_task(
        TaskRecord(
            task_id="A", project_id="EFAH-001", title="a", state=TaskState.READY, depends_on=("B",)
        )
    )
    control_plane.upsert_task(
        TaskRecord(
            task_id="B", project_id="EFAH-001", title="b", state=TaskState.READY, depends_on=("A",)
        )
    )
    graph = control_plane.graph("EFAH-001")
    assert graph.has_cycle is True


def test_resume_refuses_a_gate_owned_state(wired, context) -> None:
    control_plane, runtime, _ = wired
    control_plane.upsert_task(
        TaskRecord(task_id="T-M", project_id="EFAH-001", title="merged", state=TaskState.MERGED)
    )
    controller = TaskController(reader=control_plane, writer=control_plane, runtime=runtime)
    with pytest.raises(GateBypassRejected):
        controller.resume(task_id="T-M", context=context)


def test_evaluation_controller_returns_status_without_content(wired) -> None:
    control_plane, _, _ = wired
    control_plane.upsert_evaluation(
        "EFAH-001",
        EvaluationRecord(
            evaluation_id="EV-1",
            visible_verdict=Verdict.PASS,
            hidden_suite_verdict=Verdict.FAIL,
            hidden_assertions_total=3,
            hidden_assertions_failed=1,
        ),
    )
    row = EvaluationController(reader=control_plane).get(evaluation_id="EV-1")
    assert row.hidden_suite_verdict is Verdict.FAIL
    assert row.hidden_assertions_failed == 1


def test_dependency_impact_comes_from_the_registry(wired) -> None:
    control_plane, _, _ = wired
    control_plane.upsert_task(
        TaskRecord(
            task_id="T-api",
            project_id="EFAH-001",
            title="api work",
            state=TaskState.READY,
            workstream="api",
            requirement_ids=("R-9",),
        )
    )
    impact = DependencyController(reader=control_plane).impact(dependency_id="fastapi")
    assert impact.affected_modules == ("api",)
    assert impact.affected_task_ids == ("T-api",)
    assert impact.affected_requirement_ids == ("R-9",)
    assert len(impact.revalidation_gate_ids) == len(set(impact.revalidation_gate_ids))


def test_dependency_impact_for_an_unknown_dependency_is_not_found(wired) -> None:
    control_plane, _, _ = wired
    with pytest.raises(NotFound):
        DependencyController(reader=control_plane).impact(dependency_id="nope")


def test_contract_approval_must_name_the_governing_revision(wired, context) -> None:
    control_plane, _, _ = wired
    controller = ContractController(writer=control_plane)
    with pytest.raises(StaleContractVersion):
        controller.approve(
            contract_id=CONTRACT_ID,
            approved_version="1.0",
            approver="owner",
            rationale="",
            context=context,
        )
    with pytest.raises(ScopeExpansionRejected):
        controller.approve(
            contract_id="OTHER-CONTRACT",
            approved_version=CONTRACT_VERSION,
            approver="owner",
            rationale="",
            context=context,
        )
    decision = controller.approve(
        contract_id=CONTRACT_ID,
        approved_version=CONTRACT_VERSION,
        approver="owner",
        rationale="signed",
        context=context,
    )
    assert decision.contract_version == CONTRACT_VERSION


def test_only_a_reaffirmed_review_advances_automatically() -> None:
    """Section 19.4."""
    assert ContractController.advances_automatically(ContractReviewOutcome.CONTRACT_REAFFIRMED)
    for outcome in ContractReviewOutcome:
        if outcome is not ContractReviewOutcome.CONTRACT_REAFFIRMED:
            assert not ContractController.advances_automatically(outcome)
