"""HTTP router (contract Section 11.3).

> The HTTP/API router maps endpoints to controllers only. It MUST NOT contain
> workflow or model-routing decisions.

So every handler below is the same four lines: resolve the controller, call one
use case, wrap the result. There is no ``if``, no state transition, no retry, no
model or alias selection, and no persistence call anywhere in this file --
``tests/unit/test_api_controllers.py`` asserts that with an AST scan, because
"we'll keep it thin" is not an enforcement mechanism.

Authorization is declared per route as a dependency rather than branched on
inside a handler, which keeps the policy visible in the route table.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.context import RequestContext, Scope, require_context
from api.controllers.contracts import ContractController
from api.controllers.dependencies import DependencyController
from api.controllers.evaluations import EvaluationController
from api.controllers.projects import ProjectController
from api.controllers.tasks import TaskController
from api.deps import (
    Container,
    get_container,
    get_contracts,
    get_dependencies,
    get_evaluations,
    get_projects,
    get_tasks,
)
from api.middleware.auth import requires
from api.schemas import (
    Acknowledgement,
    ApproveContractCommand,
    HealthResponse,
    ImportProjectCommand,
    ResumeTaskCommand,
    ReviewContractCommand,
    RunProjectCommand,
)
from api.state import GraphView, ImpactMap
from dashboard.views import DashboardProjection, EvaluationStatusRow, ScopeDriftFindings

router = APIRouter()


def _ack(detail: str, payload: dict, context: RequestContext) -> Acknowledgement:
    return Acknowledgement(
        accepted=True,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        detail=detail,
        payload=payload,
    )


# ---------------------------------------------------------------------- health


@router.get("/health", response_model=HealthResponse, tags=["ops"])
def health(container: Container = Depends(get_container)) -> HealthResponse:
    """Section 5.2 health check. Reports wiring facts, not a hard-coded ``ok``."""
    from integrations.otel import installed_provider

    return HealthResponse(
        status="ok",
        projects_loaded=len(container.reader.list_project_ids()),
        runtime_executes_graph=bool(getattr(container.runtime, "executes_graph", True)),
        projection_available=(
            None if container.projection is None else container.projection.is_available()
        ),
        tracing_installed=installed_provider() is not None,
    )


# -------------------------------------------------------------------- projects


@router.post("/projects/import", response_model=Acknowledgement, tags=["projects"])
def import_project(
    command: ImportProjectCommand,
    controller: ProjectController = Depends(get_projects),
    _: object = Depends(requires(Scope.PROJECT_WRITE)),
) -> Acknowledgement:
    context = require_context()
    project = controller.import_project(pack_root=command.pack_root, context=context)
    return _ack("project imported", project.model_dump(mode="json"), context)


@router.post("/projects/{project_id}/run", response_model=Acknowledgement, tags=["projects"])
def run_project(
    project_id: str,
    command: RunProjectCommand,
    controller: ProjectController = Depends(get_projects),
    _: object = Depends(requires(Scope.PROJECT_WRITE)),
) -> Acknowledgement:
    context = require_context()
    handle = controller.run(project_id=project_id, context=context, reason=command.reason)
    return _ack("run accepted", handle.model_dump(mode="json"), context)


@router.get("/projects/{project_id}/status", response_model=DashboardProjection, tags=["projects"])
def project_status(
    project_id: str,
    controller: ProjectController = Depends(get_projects),
    _: object = Depends(requires(Scope.PROJECT_READ)),
) -> DashboardProjection:
    return controller.status(project_id=project_id)


@router.get("/projects/{project_id}/graph", response_model=GraphView, tags=["projects"])
def project_graph(
    project_id: str,
    controller: ProjectController = Depends(get_projects),
    _: object = Depends(requires(Scope.PROJECT_READ)),
) -> GraphView:
    return controller.graph(project_id=project_id)


@router.get(
    "/projects/{project_id}/scope-drift", response_model=ScopeDriftFindings, tags=["projects"]
)
def project_scope_drift(
    project_id: str,
    controller: ProjectController = Depends(get_projects),
    _: object = Depends(requires(Scope.PROJECT_READ)),
) -> ScopeDriftFindings:
    return controller.scope_drift(project_id=project_id)


# ----------------------------------------------------------------------- tasks


@router.get("/tasks/{task_id}", tags=["tasks"])
def get_task(
    task_id: str,
    controller: TaskController = Depends(get_tasks),
    _: object = Depends(requires(Scope.TASK_READ)),
) -> dict:
    return controller.get(task_id=task_id)


@router.post("/tasks/{task_id}/resume", response_model=Acknowledgement, tags=["tasks"])
def resume_task(
    task_id: str,
    command: ResumeTaskCommand,
    controller: TaskController = Depends(get_tasks),
    _: object = Depends(requires(Scope.TASK_WRITE)),
) -> Acknowledgement:
    context = require_context()
    handle = controller.resume(
        task_id=task_id,
        context=context,
        reason=command.reason,
        owner_answer=command.owner_answer,
    )
    return _ack("resume accepted", handle.model_dump(mode="json"), context)


# ----------------------------------------------------------------- evaluations


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationStatusRow, tags=["evaluations"])
def get_evaluation(
    evaluation_id: str,
    controller: EvaluationController = Depends(get_evaluations),
    _: object = Depends(requires(Scope.EVALUATION_READ)),
) -> EvaluationStatusRow:
    return controller.get(evaluation_id=evaluation_id)


# ---------------------------------------------------------------- dependencies


@router.get("/dependencies/{dependency_id}/impact", response_model=ImpactMap, tags=["dependencies"])
def dependency_impact(
    dependency_id: str,
    controller: DependencyController = Depends(get_dependencies),
    _: object = Depends(requires(Scope.DEPENDENCY_READ)),
) -> ImpactMap:
    return controller.impact(dependency_id=dependency_id)


# ------------------------------------------------------------------- contracts


@router.post("/contracts/{contract_id}/approve", response_model=Acknowledgement, tags=["contracts"])
def approve_contract(
    contract_id: str,
    command: ApproveContractCommand,
    controller: ContractController = Depends(get_contracts),
    _: object = Depends(requires(Scope.CONTRACT_APPROVE, Scope.OWNER_DECIDE)),
) -> Acknowledgement:
    context = require_context()
    decision = controller.approve(
        contract_id=contract_id,
        approved_version=command.approved_version,
        approver=command.approver,
        rationale=command.rationale,
        context=context,
    )
    return _ack("contract approval recorded", decision.model_dump(mode="json"), context)


@router.post("/contracts/{contract_id}/review", response_model=Acknowledgement, tags=["contracts"])
def review_contract(
    contract_id: str,
    command: ReviewContractCommand,
    controller: ContractController = Depends(get_contracts),
    _: object = Depends(requires(Scope.CONTRACT_REVIEW)),
) -> Acknowledgement:
    context = require_context()
    decision = controller.review(
        contract_id=contract_id,
        project_id=command.project_id,
        outcome=command.outcome,
        reviewer=command.reviewer,
        notes=command.notes,
        context=context,
    )
    return _ack("contract review recorded", decision.model_dump(mode="json"), context)
