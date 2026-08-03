"""Composition container and FastAPI dependencies (contract Section 5.1).

> A composition root MUST show how every required module is constructed and
> registered.

:class:`Container` is that showing: one object, constructed once in
:func:`api.app.create_app`, holding every port implementation and every
controller. Nothing in the request path constructs a dependency; handlers ask
the container for a controller and nothing else.

Swapping an implementation is therefore a call-site change at the composition
root, not an edit inside a controller:

.. code-block:: python

    container = Container.build(
        control_plane=TerminusControlPlane(...),   # WS-B
        runtime=LangGraphRuntime(...),             # WS-C
        projection=PlaneProjection.from_pack(...), # this workstream
    )
    app = create_app(container=container)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request

from api.adapters.control_plane_memory import InMemoryControlPlane, RecordingRuntime
from api.controllers.contracts import ContractController
from api.controllers.dependencies import DependencyController
from api.controllers.evaluations import EvaluationController
from api.controllers.projects import ContractDriftEngine, ProjectController
from api.controllers.tasks import TaskController
from api.middleware.audit import AuditSink
from api.ports import (
    ControlPlaneReadPort,
    ControlPlaneWritePort,
    DriftEnginePort,
    ProjectionPort,
    RuntimePort,
)
from dashboard.source import ReadOnlySource


@dataclass
class Container:
    """Everything the API needs, constructed once."""

    control_plane: Any
    runtime: RuntimePort
    drift_engine: DriftEnginePort
    projection: ProjectionPort | None
    audit_sink: AuditSink

    projects: ProjectController
    tasks: TaskController
    evaluations: EvaluationController
    dependencies: DependencyController
    contracts: ContractController

    @property
    def reader(self) -> ControlPlaneReadPort:
        return self.control_plane

    @property
    def writer(self) -> ControlPlaneWritePort:
        return self.control_plane

    @property
    def read_only(self) -> ReadOnlySource:
        """The handle the dashboard and Plane projection are given."""
        return ReadOnlySource(self.control_plane)

    @classmethod
    def build(
        cls,
        *,
        control_plane: Any | None = None,
        runtime: RuntimePort | None = None,
        drift_engine: DriftEnginePort | None = None,
        projection: ProjectionPort | None = None,
        audit_sink: AuditSink | None = None,
    ) -> Container:
        plane = control_plane if control_plane is not None else InMemoryControlPlane()
        resolved_runtime = runtime if runtime is not None else RecordingRuntime(plane)
        resolved_drift = drift_engine if drift_engine is not None else ContractDriftEngine(plane)
        return cls(
            control_plane=plane,
            runtime=resolved_runtime,
            drift_engine=resolved_drift,
            projection=projection,
            audit_sink=audit_sink or AuditSink(),
            projects=ProjectController(
                reader=plane, writer=plane, runtime=resolved_runtime, drift_engine=resolved_drift
            ),
            tasks=TaskController(reader=plane, writer=plane, runtime=resolved_runtime),
            evaluations=EvaluationController(reader=plane),
            dependencies=DependencyController(reader=plane),
            contracts=ContractController(writer=plane),
        )


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("the application was created without a composition container")
    return container


def get_projects(request: Request) -> ProjectController:
    return get_container(request).projects


def get_tasks(request: Request) -> TaskController:
    return get_container(request).tasks


def get_evaluations(request: Request) -> EvaluationController:
    return get_container(request).evaluations


def get_dependencies(request: Request) -> DependencyController:
    return get_container(request).dependencies


def get_contracts(request: Request) -> ContractController:
    return get_container(request).contracts
