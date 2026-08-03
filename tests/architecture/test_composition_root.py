"""Contract §5.1/§5.2 — the composition root is the wiring proof.

These tests are about the *declaration*, not the live run: they must pass in CI
with no TerminusDB, no LiteLLM, and no tailnet. The live end-to-end pass lives in
tests/e2e/test_walking_skeleton.py and is opt-in.
"""
from __future__ import annotations

from composition.root import E2E, build_registry


def test_every_declared_module_is_reachable_from_an_entrypoint():
    """The §5.2 failure: a module that exists but is on no execution path.

    An earlier version of build_registry declared twelve modules that nothing
    consumed. verify() reported every one -- the registry catching its own
    author is the strongest evidence it is not decorative.
    """
    findings = build_registry().verify(entrypoints={"composition", "cli"})
    assert findings == [], [f"{f.module}: {f.kind} — {f.detail}" for f in findings]


def test_every_contract_section_5_module_is_registered():
    """§5's layout is not advisory: each domain module must be constructed."""
    declared = set(build_registry().declarations)
    for module in (
        "api", "projects", "planning", "contracts", "requirements", "methodologies",
        "research", "evidence", "dependencies", "tasks", "assignments", "artifacts",
        "models", "workers", "workflows", "evaluation", "oracles", "holdouts",
        "mutants", "gold", "knowledge", "ontology", "governance", "provenance",
        "drift", "impact", "observability", "dashboard", "integrations", "composition",
    ):
        assert module in declared, f"§5 module {module!r} is not registered in the composition root"


def test_every_module_declares_the_full_section_5_2_block():
    for module, decl in build_registry().declarations.items():
        assert decl.missing_fields() == [], f"{module}: {decl.missing_fields()}"
        assert decl.e2e_path == E2E


def test_no_circular_dependency_between_modules():
    assert build_registry().cycles() == []


def test_the_owner_surface_is_on_the_end_to_end_path():
    """AMENDMENT-001 added a station; it must not be an island."""
    registry = build_registry()
    assert "owner_control" in registry.declarations["composition"].consumes
    assert "http_surface" in registry.declarations["owner_surface"].consumes


def test_removing_a_consumer_makes_a_module_unreachable():
    """Negative control: the verifier must actually reject something."""
    registry = build_registry()
    registry.declarations["composition"].consumes = [
        c for c in registry.declarations["composition"].consumes if c != "evidence_dossier"
    ]
    registry.declarations["dashboard"].consumes = [
        c for c in registry.declarations["dashboard"].consumes if c != "evidence_dossier"
    ]
    findings = registry.verify(entrypoints={"composition", "cli"})
    assert any(f.module == "evidence" and f.kind == "MISSING_WIRING" for f in findings)
