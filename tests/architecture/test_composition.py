"""Contract Section 5.2 wiring completion rule and ORACLE-001 reachability.

The negative controls matter more than the positive one: a composition verifier
that has never rejected anything is not a verifier.
"""
from __future__ import annotations

from composition.registry import ModuleRegistry, WiringDeclaration


def complete(module: str, provides=(), consumes=()) -> WiringDeclaration:
    return WiringDeclaration(
        module=module, provides=list(provides), consumes=list(consumes),
        startup_registration=True,
        configuration_schema=f"{module}.config",
        health_check=f"/health/{module}",
        integration_test=f"tests/integration/test_{module}.py",
        e2e_path="project-pack-import-to-owner-surface",
        telemetry_span=f"efah.{module}",
        dashboard_projection=f"views.{module}",
    )


def test_a_fully_wired_registry_passes():
    r = ModuleRegistry(root_provides={"terminusdb"})
    r.register(complete("api", provides=["http"], consumes=["tasks"]))
    r.register(complete("tasks", provides=["tasks"], consumes=["terminusdb"]))
    assert r.verify(entrypoints={"api"}) == []


def test_unit_tested_but_unregistered_module_fails():
    """Section 5.2: passing unit tests is not completion."""
    r = ModuleRegistry()
    decl = complete("drift", provides=["drift"])
    decl.startup_registration = False
    r.register(decl)
    findings = r.incomplete_modules()
    assert findings and "startup_registration" in findings[0].detail


def test_module_missing_e2e_path_fails():
    r = ModuleRegistry()
    decl = complete("gold", provides=["gold"])
    decl.e2e_path = None
    r.register(decl)
    assert any("e2e_path" in f.detail for f in r.incomplete_modules())


def test_unresolved_consumer_is_missing_wiring():
    r = ModuleRegistry()
    r.register(complete("api", provides=["http"], consumes=["nonexistent"]))
    findings = r.unresolved_consumers()
    assert findings and findings[0].kind == "MISSING_WIRING"


def test_circular_dependency_is_rejected():
    """Section 5.1: architecture tests MUST reject circular dependencies."""
    r = ModuleRegistry()
    r.register(complete("a", provides=["A"], consumes=["B"]))
    r.register(complete("b", provides=["B"], consumes=["A"]))
    assert any(f.kind == "CIRCULAR_DEPENDENCY" for f in r.cycles())


def test_orphan_module_is_unreachable():
    """The exact GATE-D2-10 case: registered but off every execution path."""
    r = ModuleRegistry()
    r.register(complete("api", provides=["http"]))
    r.register(complete("orphan", provides=["orphan"]))
    findings = r.unreachable_modules(entrypoints={"api"})
    assert [f.module for f in findings] == ["orphan"]
    assert findings[0].kind == "MISSING_WIRING"
