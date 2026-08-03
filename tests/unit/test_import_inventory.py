"""The AST sweep that answers "what actually imports what", on a synthetic tree.

``composition.inventory`` is read by two callers with opposite filters --
ORACLE-001 wants the first-party edges, the Section 16.3 dependency registry
wants the third-party ones -- so the sweep is exercised here against a tree
built for the purpose rather than against ``src/``. A test that asserts against
the real tree is a test that a source edit can break for reasons that have
nothing to do with the scanner.
"""

from __future__ import annotations

import sys

import pytest

from composition.inventory import (
    first_party_modules,
    first_party_packages,
    import_edges,
    third_party_import_sites,
)


@pytest.fixture
def tree(tmp_path):
    """Two first-party packages, one nested, importing a bit of everything."""
    alpha = tmp_path / "alpha"
    (alpha / "nested").mkdir(parents=True)
    (alpha / "__init__.py").write_text("import httpx\n")
    (alpha / "client.py").write_text(
        "import json\n"
        "import httpx\n"
        "from beta.helpers import thing\n"
        "from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver\n"
        "from . import sibling\n"
    )
    (alpha / "nested" / "__init__.py").write_text("")
    (alpha / "nested" / "deep.py").write_text("import langgraph.graph\n")

    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / "__init__.py").write_text("")
    (beta / "helpers.py").write_text("from pydantic import BaseModel\n")

    # Not a package: no __init__.py, so nothing under it is first-party.
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "script.py").write_text("import numpy\n")
    return tmp_path


def test_first_party_packages_needs_an_init_file(tree):
    assert first_party_packages(tree) == ["alpha", "beta"]


def test_first_party_modules_are_dotted_and_name_packages_by_the_package(tree):
    """``alpha/__init__.py`` is ``alpha`` -- the name an importer would write."""
    assert first_party_modules(tree) == [
        "alpha",
        "alpha.client",
        "alpha.nested",
        "alpha.nested.deep",
        "beta",
        "beta.helpers",
    ]


def test_third_party_sites_keep_the_full_dotted_name(tree):
    """Root-only keys collapse distributions that publish into one namespace."""
    sites = third_party_import_sites(tree)
    assert sites["langgraph.checkpoint.sqlite.aio"] == ["alpha.client"]
    assert sites["langgraph.graph"] == ["alpha.nested.deep"]


def test_third_party_sites_exclude_first_party_and_stdlib(tree):
    sites = third_party_import_sites(tree)
    assert "beta.helpers" not in sites, "a first-party import is not a dependency"
    assert "json" not in sites, "the stdlib is not a dependency"
    assert set(sites) == {
        "httpx",
        "langgraph.checkpoint.sqlite.aio",
        "langgraph.graph",
        "pydantic",
    }


def test_a_module_outside_a_package_contributes_nothing(tree):
    assert "numpy" not in third_party_import_sites(tree)


def test_importers_are_listed_once_and_sorted(tree):
    assert third_party_import_sites(tree)["httpx"] == ["alpha", "alpha.client"]


def test_relative_imports_carry_no_edge(tree):
    """``from . import sibling`` is intra-package: no cross-module dependency."""
    assert import_edges(tree) == [("alpha", "beta")]


def test_an_unparseable_file_does_not_fail_the_sweep(tree):
    (tree / "alpha" / "broken.py").write_text("def (:\n")
    assert third_party_import_sites(tree)["httpx"] == ["alpha", "alpha.client"]


def test_stdlib_membership_comes_from_the_interpreter():
    """Not a frozen list: a hand-written one goes stale every Python release."""
    assert "tomllib" in sys.stdlib_module_names
    assert "httpx" not in sys.stdlib_module_names
