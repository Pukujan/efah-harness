"""What is on disk, and what actually imports what.

ORACLE-001 decides reachability from the edges it is handed. Today those edges
come from :func:`composition.root.build_registry` -- the ``consumes`` column of
a hand-written table -- and ``checks_audit_followup._snapshot_from_registry``
passes the same list as both ``invocation_edges`` and ``import_edges``. So the
oracle's "independent second checker" is a copy of the first, and a capability
string is doing the work an import was supposed to do.

``root.build_registry`` says of that table: *"The edges below are the REAL
execution path, not an aspirational diagram."* This module is how that sentence
becomes checkable rather than asserted. A declared capability is a **claim**; an
import found here is a **fact**. GP-001 already says registration is not
reachability, and a table of capability names is registration wearing
reachability's coat.

Two things no hand-maintained table can answer, and this module answers both by
reading ``src/``: which first-party packages exist, and which of them import
which. Nothing here decides a verdict -- it supplies the subject ORACLE-001
judges, so the oracle body and its mint are untouched.

The same walk answers a third question for a different caller. Contract Section
16.3 requires every dependency to record *modules and contracts using it*, and
:mod:`provenance.importer` had been answering it with the dependency's own name.
:func:`third_party_import_sites` inverts the first-party filter -- everything
imported that is neither first-party nor stdlib -- so the registry is populated
from the same facts the oracle is judged on rather than from a second
declaration. Distribution names are deliberately *not* resolved here: mapping an
import root to a PyPI distribution is packaging knowledge, and this module only
knows what is written in the source.

Stdlib only, deliberately: :mod:`oracles.no_judge` walks the import closure of
anything on a verdict path and fails on a network or model root.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

__all__ = [
    "SRC",
    "edge_sites",
    "first_party_modules",
    "first_party_packages",
    "import_edges",
    "third_party_import_sites",
]

#: ``src/`` -- this file is ``src/composition/inventory.py``.
SRC = Path(__file__).resolve().parents[1]

#: Directory names under ``src/`` that are not first-party domain modules.
_NOT_A_MODULE = frozenset({"__pycache__"})


def first_party_packages(src: Path | None = None) -> list[str]:
    """Every importable first-party package directly under ``src/``.

    A package is a directory holding ``__init__.py``. Build metadata
    (``*.egg-info``) and caches are excluded; nothing else is, because the
    point is to see what is really there rather than what was declared.
    """
    root = SRC if src is None else src
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and entry.name not in _NOT_A_MODULE
        and not entry.name.endswith(".egg-info")
        and (entry / "__init__.py").is_file()
    )


def _dotted_imports(path: Path) -> set[str]:
    """Full dotted names imported by one file, not merely their roots.

    ``from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver`` yields
    ``langgraph.checkpoint.sqlite.aio``. The depth matters to a caller that has
    to tell distributions apart: three separately versioned ones publish into
    the ``langgraph`` namespace, and a root-only record collapses them.

    Relative imports (``level > 0``) are intra-package and carry no
    cross-module edge, so they are skipped rather than resolved. A file that
    will not parse contributes nothing instead of failing the sweep -- a syntax
    error is a real problem, but it is not this function's to report.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _top_level_imports(path: Path) -> set[str]:
    """Root package names imported by one file."""
    return {name.split(".")[0] for name in _dotted_imports(path)}


def _module_name(relative: Path) -> str:
    """``api/middleware/audit.py`` -> ``api.middleware.audit``.

    A package's ``__init__.py`` is named for the package itself, so
    ``workflows/graphs/__init__.py`` is ``workflows.graphs`` rather than
    ``workflows.graphs.__init__``. That is the name an importer would write.
    """
    parts = relative.parts[:-1]
    if relative.stem != "__init__":
        parts += (relative.stem,)
    return ".".join(parts)


def _source_files(root: Path, packages: set[str]) -> list[tuple[str, Path]]:
    """``(dotted module name, path)`` for every first-party source file."""
    return [
        (_module_name(path.relative_to(root)), path)
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(root).parts[0] in packages
    ]


def first_party_modules(src: Path | None = None) -> list[str]:
    """Every first-party module under ``src/``, by dotted name.

    Package granularity is too coarse for a dependency registry: knowing that
    ``api`` uses starlette does not say whether one middleware does or all ten.
    """
    root = SRC if src is None else src
    return sorted(name for name, _ in _source_files(root, set(first_party_packages(root))))


def third_party_import_sites(src: Path | None = None) -> dict[str, list[str]]:
    """``imported dotted name -> the first-party modules that import it``.

    The complement of :func:`edge_sites`: everything imported that is neither
    first-party nor stdlib, keyed by the name as written rather than by
    distribution. ``sys.stdlib_module_names`` is the interpreter's own answer
    to "is this the standard library", which is the only answer that stays
    right across Python versions.

    This is evidence, not attribution. Deciding that
    ``langgraph.checkpoint.sqlite.aio`` belongs to the
    ``langgraph-checkpoint-sqlite`` distribution needs packaging knowledge this
    module does not have and should not acquire; the caller holding the
    component-to-distribution map does that.
    """
    root = SRC if src is None else src
    packages = set(first_party_packages(root))
    sites: dict[str, set[str]] = {}
    for module, path in _source_files(root, packages):
        for imported in _dotted_imports(path):
            if imported.split(".")[0] in packages or imported.split(".")[0] in sys.stdlib_module_names:
                continue
            sites.setdefault(imported, set()).add(module)
    return {imported: sorted(modules) for imported, modules in sorted(sites.items())}


def edge_sites(src: Path | None = None) -> dict[tuple[str, str], list[str]]:
    """``(importer, imported) -> the files that create the edge``.

    The file list is the evidence. A finding that says "this declared edge is
    backed by no import" is only actionable next to the ones that *are*, so the
    caller can see what a real edge looks like in this tree.
    """
    root = SRC if src is None else src
    packages = set(first_party_packages(root))
    sites: dict[tuple[str, str], list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        importer = relative.parts[0]
        if importer not in packages:
            continue
        for imported in _top_level_imports(path):
            if imported in packages and imported != importer:
                sites.setdefault((importer, imported), []).append(relative.as_posix())
    return sites


def import_edges(src: Path | None = None) -> list[tuple[str, str]]:
    """Package-level ``(importer, imported)`` edges, first-party only.

    Same direction as the registry's capability edges -- consumer first -- so
    the two are directly comparable without inverting either.
    """
    return sorted(edge_sites(src))
