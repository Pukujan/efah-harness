"""Structural proof that no model judge participates in a verdict path.

Contract Section 17.4 requires *structural proof*, not an assurance. A comment
saying "this function makes no model call" is worth nothing; an import-closure
analysis that fails the build when someone adds ``httpx`` to an oracle module
is worth something. GATE-D2-20 A2 (``call_graph_analysis``,
``zero_model_calls_in_verdict_path``) is exactly this check.

The analysis is conservative in the direction that matters. It walks the
*transitive first-party import closure* of the module implementing the verdict
path and fails on any module that can reach a network client, a model gateway,
or a judge. Reaching a model requires either a network library or a first-party
module that owns one; both are enumerated below. A module that imports nothing
outside ``governance``, ``pydantic`` and the standard library cannot call a
model, and that is provable by reading its imports.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Third-party roots that can reach a network, and therefore a model.
NETWORK_ROOTS = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "urllib3",
        "http",
        "socket",
        "websockets",
        "grpc",
        "openai",
        "anthropic",
        "litellm",
        "google",
        "boto3",
        "langchain",
        "langgraph",
        "langchain_core",
    }
)

#: First-party packages that own model access or judgement. An oracle that
#: imports one of these has a path to a model even if it never calls it.
FIRST_PARTY_MODEL_ROOTS = frozenset({"models", "workers", "research"})

#: Attribute chains that name a model call even when the import is indirect.
MODEL_CALL_ATTRIBUTES = frozenset(
    {"chat", "completions", "messages", "generate", "invoke", "judge", "adjudicate"}
)


@dataclass
class NoJudgeProof:
    """The evidence artifact GATE-D2-20 A2 needs."""

    entry_module: str
    modules_in_closure: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def holds(self) -> bool:
        return not self.violations

    def as_evidence(self) -> dict[str, object]:
        return {
            "check": "call_graph_analysis",
            "expected": "zero_model_calls_in_verdict_path",
            "entry_module": self.entry_module,
            "modules_analysed": sorted(self.modules_in_closure),
            "network_roots_forbidden": sorted(NETWORK_ROOTS),
            "first_party_model_roots_forbidden": sorted(FIRST_PARTY_MODEL_ROOTS),
            "violations": self.violations,
            "holds": self.holds,
        }


def _source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _module_path(module: str, root: Path) -> Path | None:
    candidate = root / Path(*module.split(".")).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = root / Path(*module.split(".")) / "__init__.py"
    if package.is_file():
        return package
    return None


def _imported_roots(tree: ast.AST) -> set[tuple[str, str]]:
    """Return ``(root, full_dotted_name)`` for every import in *tree*."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the same package
                continue
            if node.module:
                found.add((node.module.split(".")[0], node.module))
    return found


def _attribute_chain_hits(tree: ast.AST) -> set[str]:
    hits: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MODEL_CALL_ATTRIBUTES
        ):
            hits.add(node.func.attr)
    return hits


def prove_no_judge(entry_module: str) -> NoJudgeProof:
    """Walk the first-party import closure of *entry_module*.

    ``entry_module`` is a dotted module name importable from ``src/`` -- for
    example ``oracles.oracle_003_provenance``.
    """
    root = _source_root()
    proof = NoJudgeProof(entry_module=entry_module)

    seen: set[str] = set()
    queue: list[str] = [entry_module]

    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module, root)
        if path is None:
            proof.violations.append(f"{module}: module source not found under {root}")
            continue
        proof.modules_in_closure.append(module)
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a syntax error fails earlier
            proof.violations.append(f"{module}: unparseable ({exc})")
            continue

        for import_root, dotted in _imported_roots(tree):
            if import_root in NETWORK_ROOTS:
                proof.violations.append(f"{module}: imports network-capable {dotted!r}")
                continue
            if import_root in FIRST_PARTY_MODEL_ROOTS:
                proof.violations.append(f"{module}: imports model-owning package {dotted!r}")
                continue
            if _module_path(dotted, root) is not None:
                queue.append(dotted)

        for attr in sorted(_attribute_chain_hits(tree)):
            proof.violations.append(f"{module}: calls .{attr}(), a model-call surface")

    return proof


def prove_many(entry_modules: list[str]) -> list[NoJudgeProof]:
    return [prove_no_judge(m) for m in entry_modules]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = [
            "oracles.oracle_001_composition",
            "oracles.oracle_002_lease_fencing",
            "oracles.oracle_003_provenance",
        ]
    failed = False
    for proof in prove_many(argv):
        status = "PROVEN" if proof.holds else "VIOLATED"
        print(f"{proof.entry_module}: {status} ({len(proof.modules_in_closure)} modules)")
        for violation in proof.violations:
            print(f"    {violation}")
        failed |= not proof.holds
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
