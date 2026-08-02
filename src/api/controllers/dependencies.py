"""Dependency impact use case (contract Sections 11.5, 16.2, 16.3)."""

from __future__ import annotations

from api.errors import NotFound
from api.ports import ControlPlaneReadPort
from api.state import ImpactMap


class DependencyController:
    """``GET /dependencies/{id}/impact``.

    Section 16.2's version-diff loop needs "what does bumping this break?"
    answered from the recorded dependency graph, not from a model's guess.
    """

    def __init__(self, *, reader: ControlPlaneReadPort) -> None:
        self._reader = reader

    def impact(self, *, dependency_id: str) -> ImpactMap:
        dependency = self._reader.get_dependency(dependency_id)
        if dependency is None:
            raise NotFound(f"dependency {dependency_id} is not in the registry")
        impact = self._reader.impact_map(dependency_id)
        if impact is None:
            raise NotFound(f"no impact map is recorded for dependency {dependency_id}")
        return impact
