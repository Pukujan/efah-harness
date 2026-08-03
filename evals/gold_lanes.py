"""Framework-free access to the Cortex objective gold lanes.

This module is the verdict path's foundation and it deliberately imports NOTHING from
`inspect_ai`. That is not stylistic: it is the structural proof that the verdict of record
is produced independently of the eval runner. If this file ever grows an `inspect_ai`
import, the separation the Eval Lab is built on has been lost.

What lives here:
  * lane resolution (where the gold lanes are on disk, overridable by env)
  * loading a lane's deterministic checker module from source
  * loading a lane's `hard_gold.jsonl`
  * two integrity scans used as ADVISORY diagnostics, never as the verdict:
      - `module_forbidden_imports` / `verdict_path_is_judge_free` — a judge/LLM/network
        AST scan, deliberately MIRRORED (not imported) from
        `/home/yoav/ssc-github/evals/oracle_adapter.py` so this package stays standalone.
      - `candidate_terminates_interpreter` — the exit-code forgery guard (see below).

THE EXIT-CODE FORGERY GUARD
---------------------------
Measured 2026-08-03 against `objective_mbpp/checker_mbpp.py`: that checker's verdict is
`subprocess returncode == 0`. The harness it builds is
`setup + candidate + assert1 + assert2 + ...` concatenated into ONE module, so a candidate
that terminates the interpreter with status 0 at import time never lets a single assert
run, and the checker reports PASS. Confirmed forging candidates, all graded PASS against
asserts they cannot satisfy:

    import sys; sys.exit(0)
    import os; os._exit(0)
    raise SystemExit(0)
    import os, atexit; atexit.register(lambda: os._exit(0))

This does NOT contaminate the recorded gold — all 460 MBPP candidates are dataset
reference solutions and deterministic AST mutations of them, neither of which can call
`sys.exit`. The exposure is prospective: the moment a real model solver is wired into this
task, a candidate can forge its own PASS. `candidate_terminates_interpreter` is a static
AST pre-scan that detects it.

It is a DIAGNOSTIC, not an override. The checker still returns the verdict (that is the
non-negotiable rule). When the guard trips on a PASS, the sample carries a
`quarantine_reason` so the result is visibly "not cleanly decidable" instead of silently
green — the same shape `OracleResult.quarantine_reason` uses upstream.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

# Where the Cortex objective lanes live. Overridable so this package is not welded to one
# machine's layout; the harness never writes into this tree, it only reads it.
DEFAULT_GOLD_LANE_ROOT = Path("/home/yoav/ssc-github/evals")


class LaneUnavailable(RuntimeError):
    """The requested gold lane is not present on this host.

    Raised rather than silently yielding an empty dataset: an eval that quietly grades
    zero samples and reports 100% is worse than one that refuses to start.
    """


def gold_lane_root() -> Path:
    return Path(os.environ.get("EFAH_GOLD_LANE_ROOT", str(DEFAULT_GOLD_LANE_ROOT)))


def lane_dir(lane: str) -> Path:
    """Directory of one objective lane, e.g. ``objective_mbpp``."""
    d = gold_lane_root() / lane
    if not d.is_dir():
        raise LaneUnavailable(f"gold lane {lane!r} not found under {gold_lane_root()}")
    return d


def read_lane_source(path: Path) -> str:
    """Read a lane source file.

    ``utf-8-sig`` on purpose: some files in the upstream lane tree are BOM-prefixed. (For
    the record, measured 2026-08-03: 0 of 213 ``evals/objective_*/*.py`` files actually
    carry a BOM today, `objective_mbpp/checker_mbpp.py` among them. Reading with
    ``utf-8-sig`` is a no-op on a BOM-less file and correct on a BOM-prefixed one, so it
    costs nothing to be right in both worlds.)
    """
    return path.read_text(encoding="utf-8-sig")


def load_checker_module(path: Path, module_name: str) -> ModuleType:
    """Import a lane checker from an absolute path, outside any package.

    The module is registered in ``sys.modules`` BEFORE execution. That is required, not
    cosmetic: `checker_mbpp.py` defines a `@dataclass` and `dataclasses._is_type` resolves
    the defining module out of `sys.modules`, so an unregistered module raises
    ``AttributeError: 'NoneType' object has no attribute '__dict__'`` at import.
    """
    if not path.is_file():
        raise LaneUnavailable(f"checker not found: {path}")
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise LaneUnavailable(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_gold_records(path: Path, limit: int | None = None) -> list[dict]:
    """Load a lane's ``hard_gold.jsonl``. Blank lines skipped; malformed lines are fatal."""
    if not path.is_file():
        raise LaneUnavailable(f"gold file not found: {path}")
    rows: list[dict] = []
    for lineno, line in enumerate(read_lane_source(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LaneUnavailable(f"{path}:{lineno} is not valid JSON: {exc}") from exc
        if limit is not None and len(rows) >= limit:
            break
    if not rows:
        raise LaneUnavailable(f"gold file is empty: {path}")
    return rows


# --------------------------------------------------------------------------- judge scan
# Mirrored from /home/yoav/ssc-github/evals/oracle_adapter.py (FORBIDDEN_VERDICT_IMPORT_
# PREFIXES). Mirrored rather than imported so `evals/` here has no cross-repo import
# dependency; `test_mbpp_execution_lane.py` asserts the two lists have not drifted when the
# upstream file is present.
FORBIDDEN_VERDICT_IMPORT_PREFIXES = (
    "cortex_core.judge",
    "cortex_core.codex_judge",
    "cortex_core.evaluator",
    "judge",
    "evaluator",
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "urllib.request",
)


def module_forbidden_imports(path: Path) -> list[str]:
    """Sorted judge/LLM/network imports found in one source file (empty == clean).

    A file that will not parse is reported as offending: a verdict-path module that cannot
    be read cannot be trusted either.
    """
    try:
        tree = ast.parse(read_lane_source(Path(path)), filename=str(path))
    except (SyntaxError, OSError):
        return ["<syntax-error-or-unreadable>"]
    hits: list[str] = []
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # `from cortex_core import judge` imports the SUBMODULE cortex_core.judge, so
            # the fully qualified "module.name" has to be checked too or a submodule
            # import silently evades the prefix match.
            mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            for bad in FORBIDDEN_VERDICT_IMPORT_PREFIXES:
                if m == bad or m.startswith(bad + "."):
                    hits.append(m)
    return sorted(set(hits))


def verdict_path_is_judge_free(modules: list[Path]) -> tuple[bool, list[str]]:
    """True iff none of the given source files import a judge/LLM/network module."""
    problems = [
        f"{p} imports forbidden {bad}"
        for p in modules
        if (bad := module_forbidden_imports(Path(p)))
    ]
    return (not problems), problems


# ------------------------------------------------------------- exit-code forgery guard
_TERMINATORS = frozenset({"exit", "_exit", "abort"})


@dataclass(frozen=True)
class IntegrityFinding:
    """One reason a candidate's PASS should not be taken at face value."""

    kind: str
    detail: str


def candidate_terminates_interpreter(code: str) -> list[IntegrityFinding]:
    """Static AST scan for constructs that can end the process before the asserts run.

    Detects `sys.exit`, `os._exit`, `os.abort`, bare `exit()`/`quit()`, `raise SystemExit`,
    and `atexit.register` (an atexit handler can `os._exit(0)` after a failing assert has
    already been swallowed... it cannot, but it CAN pre-empt a non-zero exit path, which is
    exactly what the measured `atexit` forgery does).

    Unparseable code returns a `syntax_error` finding: it is not evidence of forgery, but a
    candidate the guard could not read must not be reported as "guard clean".

    Deliberately conservative and static — it cannot see `getattr(sys, "e" + "xit")()`. It
    is a diagnostic tripwire, not a sandbox. The real fix belongs upstream in the lane
    (assert-execution witness / per-assert exit accounting), which this task may not make.
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [IntegrityFinding("syntax_error", f"candidate does not parse: {exc}")]

    findings: list[IntegrityFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                base = getattr(func.value, "id", None)
                if func.attr in _TERMINATORS and base in {"sys", "os"}:
                    findings.append(
                        IntegrityFinding("process_exit_call", f"{base}.{func.attr}(...)")
                    )
                elif func.attr == "register" and base == "atexit":
                    findings.append(IntegrityFinding("atexit_register", "atexit.register(...)"))
            elif isinstance(func, ast.Name) and func.id in {"exit", "quit"}:
                findings.append(IntegrityFinding("process_exit_call", f"{func.id}()"))
        elif isinstance(node, ast.Raise):
            exc_node = node.exc
            name = None
            if isinstance(exc_node, ast.Name):
                name = exc_node.id
            elif isinstance(exc_node, ast.Call) and isinstance(exc_node.func, ast.Name):
                name = exc_node.func.id
            if name == "SystemExit":
                findings.append(IntegrityFinding("process_exit_call", "raise SystemExit"))
    # stable, de-duplicated
    return sorted(set(findings), key=lambda f: (f.kind, f.detail))
