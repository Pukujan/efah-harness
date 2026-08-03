"""Contract Section 5.1 — architecture tests reject prohibited imports."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PROHIBITED = {"temporalio", "temporal", "anthropic", "claude_agent_sdk"}


def _module_imports(path):
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return set()
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_prohibited_dependency_anywhere():
    """DEC-001: Temporal is a non-goal and must not enter the runtime."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.parts[-2] == "adapters":
            continue
        bad = _module_imports(path) & PROHIBITED
        if bad:
            offenders.append(f"{path.relative_to(SRC)}: {sorted(bad)}")
    assert not offenders, offenders


def test_domain_does_not_import_another_modules_infrastructure():
    offenders = []
    for path in SRC.rglob("domain/**/*.py"):
        for imported in _module_imports(path):
            if imported == "infrastructure":
                offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
