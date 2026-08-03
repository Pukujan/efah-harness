"""Module registry and the wiring completion rule.

Contract EFAH-CONTRACT-001 v1.1 §5.1 and §5.2.

§5.2 is the clause that stops "the module has unit tests, therefore it is done".
A module is complete only when it *declares and proves* the eight properties
below, and the composition verifier fails when a module exists but is not
reachable through an approved user-to-result execution path.

This is the mechanism behind the observed failure "modules built but not wired"
(§26). It is deliberately data-driven: a module cannot claim wiring it did not
declare, and it cannot declare wiring the verifier cannot confirm.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class WiringDeclaration(BaseModel):
    """The §5.2 declaration every domain module must make."""

    model_config = ConfigDict(extra="forbid")

    module: str
    provides: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    startup_registration: bool = False
    configuration_schema: str | None = None
    health_check: str | None = None
    integration_test: str | None = None
    e2e_path: str | None = None
    telemetry_span: str | None = None
    dashboard_projection: str | None = None

    def missing_fields(self) -> list[str]:
        """Which §5.2 obligations this module has not met."""
        missing: list[str] = []
        if not self.startup_registration:
            missing.append("startup_registration")
        for name in (
            "configuration_schema",
            "health_check",
            "integration_test",
            "e2e_path",
            "telemetry_span",
            "dashboard_projection",
        ):
            if not getattr(self, name):
                missing.append(name)
        return missing


@dataclass
class CompositionFinding:
    module: str
    kind: str
    detail: str


@dataclass
class ModuleRegistry:
    """Every module the composition root constructs and registers."""

    declarations: dict[str, WiringDeclaration] = field(default_factory=dict)
    #: Capabilities the composition root itself satisfies (adapters to external
    #: systems that no domain module owns).
    root_provides: set[str] = field(default_factory=set)

    def register(self, declaration: WiringDeclaration) -> None:
        self.declarations[declaration.module] = declaration

    # -- verification --------------------------------------------------------

    def _provider_index(self) -> dict[str, str]:
        index: dict[str, str] = {cap: "<composition-root>" for cap in self.root_provides}
        for module, decl in self.declarations.items():
            for capability in decl.provides:
                index[capability] = module
        return index

    def unresolved_consumers(self) -> list[CompositionFinding]:
        """A module consuming a capability nothing provides is not wired."""
        provided = self._provider_index()
        findings: list[CompositionFinding] = []
        for module, decl in self.declarations.items():
            for capability in decl.consumes:
                if capability not in provided:
                    findings.append(
                        CompositionFinding(
                            module=module,
                            kind="MISSING_WIRING",
                            detail=f"consumes {capability!r}, which nothing provides",
                        )
                    )
        return findings

    def incomplete_modules(self) -> list[CompositionFinding]:
        findings: list[CompositionFinding] = []
        for module, decl in self.declarations.items():
            missing = decl.missing_fields()
            if missing:
                findings.append(
                    CompositionFinding(
                        module=module,
                        kind="MISSING_WIRING",
                        detail=f"§5.2 declaration incomplete: {', '.join(missing)}",
                    )
                )
        return findings

    def cycles(self) -> list[CompositionFinding]:
        """§5.1 — architecture tests must reject circular dependencies."""
        provided = self._provider_index()
        edges: dict[str, set[str]] = {m: set() for m in self.declarations}
        for module, decl in self.declarations.items():
            for capability in decl.consumes:
                producer = provided.get(capability)
                if producer and producer in edges and producer != module:
                    edges[module].add(producer)

        colour: dict[str, int] = dict.fromkeys(edges, 0)
        findings: list[CompositionFinding] = []

        def visit(node: str, path: list[str]) -> None:
            colour[node] = 1
            for nxt in sorted(edges[node]):
                if colour[nxt] == 1:
                    cycle = " → ".join([*path[path.index(nxt):], nxt]) if nxt in path else f"{node} → {nxt}"
                    findings.append(
                        CompositionFinding(module=node, kind="CIRCULAR_DEPENDENCY", detail=cycle)
                    )
                elif colour[nxt] == 0:
                    visit(nxt, [*path, nxt])
            colour[node] = 2

        for node in sorted(edges):
            if colour[node] == 0:
                visit(node, [node])
        return findings

    def unreachable_modules(self, *, entrypoints: set[str]) -> list[CompositionFinding]:
        """Modules not reachable from an approved user-to-result path.

        This is the §5.2 failure the composition verifier exists to catch: "a
        module exists but is not reachable through an approved user-to-result
        execution path".
        """
        provided = self._provider_index()
        reachable: set[str] = set()
        queue: deque[str] = deque(m for m in entrypoints if m in self.declarations)
        reachable.update(queue)
        while queue:
            module = queue.popleft()
            for capability in self.declarations[module].consumes:
                producer = provided.get(capability)
                if producer in self.declarations and producer not in reachable:
                    reachable.add(producer)
                    queue.append(producer)
        return [
            CompositionFinding(
                module=module,
                kind="MISSING_WIRING",
                detail="module is registered but not reachable from any entrypoint",
            )
            for module in sorted(set(self.declarations) - reachable)
        ]

    def verify(self, *, entrypoints: set[str]) -> list[CompositionFinding]:
        """All composition findings. Empty means the wiring rule is satisfied."""
        return [
            *self.incomplete_modules(),
            *self.unresolved_consumers(),
            *self.cycles(),
            *self.unreachable_modules(entrypoints=entrypoints),
        ]
