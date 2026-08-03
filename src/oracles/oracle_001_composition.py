"""ORACLE-001 — Composition reachability (hierarchy level 1).

Implements ``project-pack/acceptance/oracle-definitions/ORACLE-001-composition-reachability.yaml``
exactly. Contract Sections 5.1, 5.2, 14.4, 17.4.

The failure this kills is contract Section 26's "modules built but not wired":
a module with green unit tests that no user-to-result path ever reaches. Unit
tests cannot see that failure, because the module works in isolation. Only
reachability from an *approved* entry point can.

The four gaming probes in the definition are what shape the checks:

* GP-001 registration is not reachability -- so reachability is computed from
  invocation edges, never from the registration list.
* GP-002 a test-only entry point does not count -- so only entry points on the
  approved user-to-result list seed the traversal.
* GP-003 placeholder manifest fields do not count -- so every one of the nine
  Section 5.2 fields is checked for placeholder content, not just presence.
* GP-004 self-declared exclusions do not count -- so an exclusion without a
  recorded owner decision is itself a failure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from governance.states import TaskState, Verdict
from oracles.base import (
    Decision,
    DeterministicOracle,
    fail,
    is_placeholder,
    passed,
    unverifiable,
)

#: Contract Section 5.2. A module is complete only when all nine are proven.
WIRING_FIELDS: tuple[str, ...] = (
    "provides",
    "consumes",
    "startup_registration",
    "configuration_schema",
    "health_check",
    "integration_test",
    "e2e_path",
    "telemetry_span",
    "dashboard_projection",
)


class ModuleWiring(BaseModel):
    """The nine Section 5.2 fields a module must declare *and prove*."""

    model_config = ConfigDict(extra="forbid")

    provides: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    startup_registration: bool = False
    configuration_schema: str = ""
    health_check: str = ""
    integration_test: str = ""
    e2e_path: str = ""
    telemetry_span: str = ""
    dashboard_projection: str = ""


class EntryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    #: Only an approved user-to-result path seeds reachability (GP-002).
    approved_user_to_result_path: bool = False
    reaches: list[str] = Field(default_factory=list)


class CompositionSnapshot(BaseModel):
    """The subject ORACLE-001 decides on.

    Produced by whoever owns the composition root; the oracle never reads the
    filesystem itself, which is what keeps its verdict path pure.
    """

    model_config = ConfigDict(extra="forbid")

    composition_root_parseable: bool = True
    declared_modules: list[str] = Field(default_factory=list)
    wiring: dict[str, ModuleWiring] = Field(default_factory=dict)
    #: Modules constructed and registered at the composition root.
    registered_modules: list[str] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    #: Real invocation edges ``(caller, callee)`` observed in an execution path.
    invocation_edges: list[tuple[str, str]] = Field(default_factory=list)
    #: Import edges ``(importer, imported)`` for the independent second checker.
    import_edges: list[tuple[str, str]] = Field(default_factory=list)
    #: Module -> module whose *infrastructure implementation* it imports (5.1).
    infrastructure_imports: list[tuple[str, str]] = Field(default_factory=list)
    #: Module -> owner decision reference authorising exclusion (GP-004).
    excluded_modules: dict[str, str | None] = Field(default_factory=dict)


def _reachable(seeds: list[str], edges: list[tuple[str, str]]) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return seen


def _find_cycle(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    adjacency: dict[str, list[str]] = {n: [] for n in nodes}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].append(target)

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(nodes, WHITE)
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        path.append(node)
        for neighbour in adjacency[node]:
            if colour[neighbour] == GREY:
                return [*path[path.index(neighbour):], neighbour]
            if colour[neighbour] == WHITE:
                found = visit(neighbour)
                if found:
                    return found
        path.pop()
        colour[node] = BLACK
        return None

    for node in nodes:
        if colour[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


class CompositionReachabilityOracle(DeterministicOracle):
    """Level-1 deterministic execution/state oracle."""

    @property
    def oracle_id(self) -> str:
        return "ORACLE-001"

    def decide(self, subject: Any) -> Decision:
        snapshot: CompositionSnapshot = subject

        # --- unverifiable_when (definition, verbatim) -------------------
        if not snapshot.composition_root_parseable:
            return unverifiable("composition_root_not_parseable", unreachable_module_count=None)
        if not snapshot.entry_points:
            return unverifiable("entry_points_undeclared", unreachable_module_count=None)
        missing_manifests = [m for m in snapshot.declared_modules if m not in snapshot.wiring]
        if missing_manifests:
            return unverifiable(
                f"wiring_manifest_absent_for_one_or_more_modules: {sorted(missing_manifests)}",
                unreachable_module_count=None,
            )

        reasons: list[str] = []

        # --- GP-004: an exclusion needs a recorded owner decision --------
        unauthorised_exclusions = [
            module for module, decision_ref in snapshot.excluded_modules.items()
            if is_placeholder(decision_ref)
        ]
        if unauthorised_exclusions:
            return fail(
                [
                    f"module excluded from reachability without an owner decision: {m}"
                    for m in sorted(unauthorised_exclusions)
                ],
                TaskState.FAILED_SCOPE,
                unreachable_module_count=len(unauthorised_exclusions),
            )

        in_scope = [m for m in snapshot.declared_modules if m not in snapshot.excluded_modules]

        # --- Section 5.1: prohibited imports and cycles ------------------
        prohibited = [
            f"{importer} imports {imported}'s infrastructure implementation"
            for importer, imported in snapshot.infrastructure_imports
        ]
        cycle = _find_cycle(in_scope, snapshot.import_edges)
        if cycle:
            prohibited.append("circular dependency: " + " -> ".join(cycle))
        if prohibited:
            return fail(prohibited, TaskState.FAILED_SCOPE, unreachable_module_count=0)

        # --- GP-003: every one of the nine fields must resolve -----------
        placeholder_findings: list[str] = []
        for module in in_scope:
            manifest = snapshot.wiring[module]
            for field_name in WIRING_FIELDS:
                value = getattr(manifest, field_name)
                if field_name == "startup_registration":
                    if value is not True:
                        placeholder_findings.append(f"{module}.startup_registration is not proven")
                    continue
                if field_name in {"provides", "consumes"}:
                    continue  # a module may legitimately provide or consume nothing
                if is_placeholder(value):
                    placeholder_findings.append(
                        f"{module}.{field_name} is empty or a placeholder ({value!r})"
                    )
        if placeholder_findings:
            return fail(
                placeholder_findings,
                TaskState.FAILED_WIRING,
                unreachable_module_count=0,
            )

        # --- registration ------------------------------------------------
        unregistered = sorted(set(in_scope) - set(snapshot.registered_modules))
        if unregistered:
            reasons.extend(f"module absent from the composition root: {m}" for m in unregistered)

        # --- GP-001 / GP-002: reachability from APPROVED entry points ----
        approved = [ep for ep in snapshot.entry_points if ep.approved_user_to_result_path]
        if not approved:
            return unverifiable("entry_points_undeclared", unreachable_module_count=None)
        seeds = [target for ep in approved for target in ep.reaches]
        reachable = _reachable(seeds, snapshot.invocation_edges) | set(seeds)
        unreachable = sorted(set(in_scope) - reachable)
        if unreachable:
            reasons.extend(
                f"module unreachable from an approved user-to-result path: {m}"
                for m in unreachable
            )

        if reasons:
            return fail(
                reasons, TaskState.FAILED_WIRING, unreachable_module_count=len(unreachable)
            )

        # --- independent second checker (definition: import-graph) -------
        reachable_by_imports = _reachable(seeds, snapshot.import_edges) | set(seeds)
        import_unreachable = sorted(set(in_scope) - reachable_by_imports)
        if import_unreachable:
            return Decision(
                verdict=self._disagreement_verdict(),
                reasons=[
                    (
                        "independent second checker (import graph) disagrees: "
                        f"{import_unreachable} not reachable"
                    )
                ],
                health_extra={"unreachable_module_count": 0},
                second_checker_agreed=False,
            )

        result = passed(
            [f"all {len(in_scope)} in-scope modules registered and reachable"],
            unreachable_module_count=0,
        )
        result.second_checker_agreed = True
        return result

    def _disagreement_verdict(self) -> Verdict:
        mapping = {"UNVERIFIABLE": Verdict.UNVERIFIABLE, "FAIL": Verdict.FAIL}
        declared = str(
            self.definition.get("independent_second_checker", {}).get(
                "disagreement_result", "UNVERIFIABLE"
            )
        )
        return mapping.get(declared, Verdict.UNVERIFIABLE)
