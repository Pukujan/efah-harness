"""Read-only access to authoritative state (contract Section 5.1).

> The dashboard MUST consume read projections, not mutate authoritative state
> directly.

Enforced by construction rather than by convention. :class:`ReadOnlySource`
wraps a control plane and exposes *only* the methods on
``api.ports.ControlPlaneReadPort``. Anything else -- ``import_project``,
``upsert_task``, ``record_decision``, an internal ``_projects`` dict -- is not
reachable through the wrapper at all, so a projection cannot call it even by
mistake, and a future edit that tries to will fail at the attribute lookup.
"""

from __future__ import annotations

from typing import Any, Final

from api.state import (
    ControlPlaneSnapshot,
    DependencyRecord,
    EvaluationRecord,
    GraphView,
    ImpactMap,
    ProjectRecord,
    TaskRecord,
)

#: Exactly the read surface. Adding a name here is the only way to widen it, and
#: a mutating method must never appear in this tuple.
READ_METHODS: Final = (
    "list_project_ids",
    "get_project",
    "snapshot",
    "get_task",
    "get_evaluation",
    "get_dependency",
    "graph",
    "impact_map",
)


class MutationAttemptedFromDashboard(RuntimeError):
    """The projection layer reached for something that is not a read."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"the dashboard may not call {name!r}: contract Section 5.1 restricts it to "
            f"read projections. Reachable reads: {', '.join(READ_METHODS)}"
        )


class ReadOnlySource:
    """A control plane with its write half amputated, not merely discouraged."""

    __slots__ = ("_inner",)

    def __init__(self, control_plane: Any) -> None:
        object.__setattr__(self, "_inner", control_plane)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally, and the only
        # normally-found names are the explicit read methods below.
        raise MutationAttemptedFromDashboard(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise MutationAttemptedFromDashboard(f"set {name}")

    # -- the read surface, spelled out ------------------------------------

    def list_project_ids(self) -> list[str]:
        return self._inner.list_project_ids()

    def get_project(self, project_id: str) -> ProjectRecord | None:
        return self._inner.get_project(project_id)

    def snapshot(self, project_id: str) -> ControlPlaneSnapshot | None:
        return self._inner.snapshot(project_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._inner.get_task(task_id)

    def get_evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        return self._inner.get_evaluation(evaluation_id)

    def get_dependency(self, dependency_id: str) -> DependencyRecord | None:
        return self._inner.get_dependency(dependency_id)

    def graph(self, project_id: str) -> GraphView | None:
        return self._inner.graph(project_id)

    def impact_map(self, dependency_id: str) -> ImpactMap | None:
        return self._inner.impact_map(dependency_id)
