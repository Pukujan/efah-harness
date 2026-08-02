"""Declared application interfaces (contract Section 5.1).

> Cross-module operations MUST use declared application interfaces or domain
> events. All external systems MUST be behind adapters. A composition root MUST
> show how every required module is constructed and registered.

These Protocols are that seam. The API owns them; other workstreams implement
them:

============================  ========================================
Port                          Implemented by
============================  ========================================
``ControlPlaneReadPort``      WS-B TerminusDB adapter (authoritative)
``ControlPlaneWritePort``     WS-B TerminusDB adapter (authoritative)
``RuntimePort``               WS-C LangGraph runtime
``DriftEnginePort``           WS-A/WS-E scope-drift engine
``ProjectionPort``            ``integrations.plane`` (this workstream)
============================  ========================================

They are ``Protocol``s, not ABCs, so an implementation does not have to import
this module to satisfy it -- which keeps the dependency arrow pointing inward
and stops WS-B's persistence adapter from becoming a compile-time dependency of
the API package.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from api.state import (
    ControlPlaneSnapshot,
    DecisionRecord,
    DependencyRecord,
    DriftFindingRecord,
    EvaluationRecord,
    GraphView,
    ImpactMap,
    ProjectRecord,
    RunHandle,
    TaskRecord,
)


@runtime_checkable
class ControlPlaneReadPort(Protocol):
    """Read side of authoritative state. No method here may mutate."""

    def list_project_ids(self) -> list[str]: ...

    def get_project(self, project_id: str) -> ProjectRecord | None: ...

    def snapshot(self, project_id: str) -> ControlPlaneSnapshot | None:
        """One consistent read of everything the thirteen views need."""

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def get_evaluation(self, evaluation_id: str) -> EvaluationRecord | None: ...

    def get_dependency(self, dependency_id: str) -> DependencyRecord | None: ...

    def graph(self, project_id: str) -> GraphView | None: ...

    def impact_map(self, dependency_id: str) -> ImpactMap | None: ...


@runtime_checkable
class ControlPlaneWritePort(Protocol):
    """Write side. Only the controllers' use cases reach it, never the router."""

    def import_project(
        self, *, pack_root: str, requested_by: str, correlation_id: str
    ) -> ProjectRecord:
        """Section 6.1 intake. Returns the imported project record."""

    def record_decision(self, decision: DecisionRecord) -> DecisionRecord:
        """Section 19.4 / 20.1. An owner decision bound to a contract version."""

    def record_contract_review(
        self, *, project_id: str, outcome: str, reviewer: str, notes: str
    ) -> DecisionRecord:
        """Section 19.3 periodic and event-triggered contract review."""

    def record_run_request(
        self, *, project_id: str, task_id: str | None, run_id: str, requested_by: str
    ) -> None:
        """Persist the fact that a run was requested, before it is dispatched."""


@runtime_checkable
class RuntimePort(Protocol):
    """The durable workflow runtime (DEC-001: LangGraph, permanently)."""

    def start_project_run(
        self, *, project_id: str, requested_by: str, correlation_id: str
    ) -> RunHandle: ...

    def resume_task(
        self, *, task_id: str, project_id: str, requested_by: str, correlation_id: str
    ) -> RunHandle:
        """Section 10.6: resume from checkpoint, not from the beginning."""


@runtime_checkable
class DriftEnginePort(Protocol):
    """Section 19.1 continuous scope comparison."""

    def findings(self, project_id: str) -> list[DriftFindingRecord]: ...

    def classify_instruction(self, *, project_id: str, instruction: str) -> DriftFindingRecord | None:
        """Return a finding when an instruction would expand scope, else ``None``.

        Used by the API before any command is executed, and by the owner control
        surface (Section 11.7) for exactly the same reason: the surface holds no
        authority the API does not already grant.
        """


@runtime_checkable
class ProjectionPort(Protocol):
    """A read-only, one-way projection sink. Plane implements this.

    There is no ``read_authoritative_state`` and no ``write_back``: contract
    Section 4.1 makes the flow ``terminusdb -> plane``, one way.
    """

    @property
    def may_mutate_authoritative_state(self) -> bool: ...

    def is_available(self) -> bool:
        """Liveness of the projection target. False is degraded, never fatal."""

    def project(self, snapshot: ControlPlaneSnapshot) -> dict[str, Any]:
        """Push the snapshot outward. MUST NOT raise on projection outage."""
