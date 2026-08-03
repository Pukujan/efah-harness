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

Stdlib only, deliberately: :mod:`oracles.no_judge` walks the import closure of
anything on a verdict path and fails on a network or model root.
"""

from __future__ import annotations

import ast
from pathlib import Path

__all__ = ["SRC", "edge_sites", "first_party_packages", "import_edges"]

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


def _top_level_imports(path: Path) -> set[str]:
    """Root package names imported by one file.

    Relative imports (``level > 0``) are intra-package and carry no
    cross-module edge, so they are skipped rather than resolved. A file that
    will not parse contributes nothing instead of failing the sweep -- a syntax
    error is a real problem, but it is not this function's to report.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


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
