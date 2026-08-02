"""``harness`` -- the one-command entry point.

Contract Section 6::

    harness project run ./project-pack --mode autonomous

Section 6.1 defines the intake sequence. This module owns steps 1, 2 and 8 of
it -- validate every schema and reference, import the contract and project, and
compile -- and hands off to the modules that own the rest:

======  ===========================================  ===================
step    behaviour                                    owner
======  ===========================================  ===================
1       validate all schemas and references          ``integrations.pack``
2       import into an isolated TerminusDB branch    WS-B (lazy import)
3-7     preflight, decisions, docs, blockers, ask    WS-B / WS-E
8       recompile the project                        ``contracts.compiler``
9-10    run the LangGraph project workflow           WS-C (lazy import)
======  ===========================================  ===================

The TerminusDB importer and the LangGraph runner belong to other workstreams and
may not be merged yet. They are resolved by name at call time, never imported at
module scope: a missing lane degrades this command to validate-compile-report
and says so, instead of failing to start. Section 6.2 forbids an ambiguous
outcome, so an unavailable lane is reported as a named, typed gap -- not as
success and not as a crash.

Exit codes map to Section 6.2 terminal project states, so CI can branch on them
without parsing prose.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.compiler import CompiledProject, compile_pack
from governance.compiler import CompilationError
from governance.states import ProjectState
from integrations.pack import PackValidationError, ProjectPack, load_pack

#: Section 6.2 terminal states -> process exit codes.
EXIT_CODES: dict[ProjectState, int] = {
    ProjectState.VERIFIED_COMPLETE: 0,
    ProjectState.RUNNING: 0,
    ProjectState.BLOCKED_OWNER_DECISION: 10,
    ProjectState.BLOCKED_EXTERNAL_ACCESS: 11,
    ProjectState.FAILED_CONTRACT: 20,
    ProjectState.FAILED_ASSURANCE: 21,
    ProjectState.FAILED_INFRASTRUCTURE: 22,
    ProjectState.CANCELED: 23,
}

#: Candidate module paths for the lanes this CLI does not own. First hit wins.
TERMINUS_IMPORTERS: tuple[tuple[str, str], ...] = (
    ("integrations.terminus", "import_pack"),
    ("integrations.terminusdb", "import_pack"),
    ("ontology.terminus", "import_pack"),
    ("provenance.terminus", "import_pack"),
)
LANGGRAPH_RUNNERS: tuple[tuple[str, str], ...] = (
    ("workflows.project_graph", "run_project"),
    ("workflows.runner", "run_project"),
    ("composition.root", "run_project"),
)


@dataclass
class LaneResult:
    """Outcome of calling into a workstream this CLI does not own."""

    name: str
    available: bool
    detail: str
    result: Any = None

    def as_body(self) -> dict[str, Any]:
        return {"lane": self.name, "available": self.available, "detail": self.detail}


@dataclass
class RunReport:
    mode: str
    pack_root: str
    project_id: str = ""
    contract_id: str = ""
    contract_version: str = ""
    pack_manifest_hash: str = ""
    validated: bool = False
    compiled: bool = False
    state: ProjectState = ProjectState.RUNNING
    compiler_summary: dict[str, Any] = field(default_factory=dict)
    lanes: list[LaneResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: the compiled project itself, for callers that want the objects. Not part
    #: of :meth:`as_body` -- the JSON report carries the summary, not 1600 objects.
    project: CompiledProject | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "command": f"harness project run {self.pack_root} --mode {self.mode}",
            "project_id": self.project_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "pack_manifest_hash": self.pack_manifest_hash,
            "pack_validated": self.validated,
            "contract_compiled": self.compiled,
            "project_state": str(self.state),
            "compiler": self.compiler_summary,
            "lanes": [lane.as_body() for lane in self.lanes],
            "problems": self.problems,
            "contract_ref": "contract.md#6,#6.1,#6.2",
        }


def _resolve(candidates: Sequence[tuple[str, str]]) -> tuple[Callable[..., Any] | None, str]:
    """Find the first importable ``module:attribute`` among *candidates*."""
    tried: list[str] = []
    for module_name, attribute in candidates:
        tried.append(f"{module_name}:{attribute}")
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        function = getattr(module, attribute, None)
        if callable(function):
            return function, f"{module_name}:{attribute}"
    return None, "none of " + ", ".join(tried)


def _terminus_import(pack: ProjectPack, compiled: CompiledProject) -> LaneResult:
    function, where = _resolve(TERMINUS_IMPORTERS)
    if function is None:
        return LaneResult(
            name="terminusdb_import",
            available=False,
            detail=(
                f"no TerminusDB importer on the path ({where}). Contract Section 6.1 step 2 is not executed; "
                "the compiled objects are not persisted to an isolated branch. This lane is WS-B's."
            ),
        )
    try:
        result = function(pack=pack, compiled=compiled)
    except Exception as exc:  # a lane failure must not crash intake
        return LaneResult(
            name="terminusdb_import",
            available=True,
            detail=f"{where} raised {type(exc).__name__}: {exc}",
        )
    return LaneResult(name="terminusdb_import", available=True, detail=f"imported via {where}", result=result)


def _langgraph_run(pack: ProjectPack, compiled: CompiledProject, mode: str) -> LaneResult:
    function, where = _resolve(LANGGRAPH_RUNNERS)
    if function is None:
        return LaneResult(
            name="langgraph_run",
            available=False,
            detail=(
                f"no LangGraph project runner on the path ({where}). Contract Section 6.1 steps 9-10 are not "
                "executed; the run stops after compilation. This lane is WS-C's."
            ),
        )
    try:
        result = function(pack=pack, compiled=compiled, mode=mode)
    except Exception as exc:
        return LaneResult(name="langgraph_run", available=True, detail=f"{where} raised {type(exc).__name__}: {exc}")
    return LaneResult(name="langgraph_run", available=True, detail=f"ran via {where}", result=result)


def run_project(pack_root: str | Path, *, mode: str = "autonomous", start_workflow: bool = True) -> RunReport:
    """Validate -> compile -> report. Never raises for a pack-level problem."""
    report = RunReport(mode=mode, pack_root=str(pack_root))

    try:
        pack = load_pack(pack_root)
    except PackValidationError as exc:
        report.state = ProjectState.FAILED_CONTRACT
        report.problems.append(f"project pack validation failed: {exc}")
        return report

    report.validated = True
    report.project_id = pack.project_id
    report.contract_id = pack.contract_id
    report.contract_version = pack.contract_version
    report.pack_manifest_hash = pack.manifest_hash

    repo_root = Path(pack.root).parent
    try:
        compiled = compile_pack(pack, repo_root=repo_root)
    except CompilationError as exc:
        report.state = exc.state
        report.problems.append(f"contract compilation failed: {exc}")
        return report
    except Exception as exc:
        report.state = ProjectState.FAILED_CONTRACT
        report.problems.append(f"contract compilation raised {type(exc).__name__}: {exc}")
        return report

    report.compiled = True
    report.project = compiled
    report.compiler_summary = compiled.summary()
    if not compiled.compiles:
        report.state = ProjectState.FAILED_CONTRACT
        report.problems.extend(f["detail"] for f in report.compiler_summary["blocking_findings"])
        return report

    report.lanes.append(_terminus_import(pack, compiled))
    if start_workflow:
        report.lanes.append(_langgraph_run(pack, compiled, mode))

    unavailable = [lane.name for lane in report.lanes if not lane.available]
    if unavailable:
        report.problems.append(
            "compiled successfully but the run did not reach a terminal project state: "
            f"lanes not on the path: {unavailable}"
        )
    report.state = ProjectState.RUNNING
    return report


# --------------------------------------------------------------------------
# argument parsing and human-readable output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Evidence-first engineering harness (EFAH-CONTRACT-001 v1.1).",
    )
    subcommands = parser.add_subparsers(dest="domain", required=True)
    project = subcommands.add_parser("project", help="project pack operations")
    actions = project.add_subparsers(dest="action", required=True)

    for name, help_text in (
        ("run", "validate, compile, and start the project workflow"),
        ("validate", "validate the project pack only"),
        ("compile", "validate and compile; do not start the workflow"),
    ):
        sub = actions.add_parser(name, help=help_text)
        sub.add_argument("pack", help="path to the project pack directory")
        sub.add_argument(
            "--mode",
            default="autonomous",
            choices=["autonomous", "supervised"],
            help="autonomy mode (contract Section 6)",
        )
        sub.add_argument("--json", action="store_true", help="emit the machine-readable report")
        sub.add_argument("--out", default=None, help="write the compiled evidence bundle to this directory")

    return parser


def _print_human(report: RunReport, compiled: CompiledProject | None) -> None:
    out = sys.stdout.write
    out(f"project        {report.project_id}\n")
    out(f"contract       {report.contract_id} v{report.contract_version}\n")
    out(f"pack manifest  {report.pack_manifest_hash}\n")
    out(f"pack validated {report.validated}\n")
    if report.compiler_summary:
        summary = report.compiler_summary
        out(f"compiled       {summary['compiled_object_count']} objects\n")
        out(f"  requirements {summary['requirements']}  criteria {summary['acceptance_criteria']}\n")
        out(f"  tasks        {summary['tasks']}  work units {summary['work_units']}\n")
        out(f"  dependencies {summary['dependency_edges']} edges, {summary['cycles']} cycles\n")
        out(f"  critical path ({summary['critical_path_length']}): {' -> '.join(summary['critical_path'])}\n")
        if summary["observations"]:
            out("  observations:\n")
            for observation in summary["observations"]:
                out(f"    - {observation['kind']}: {observation['detail']}\n")
        if summary["blocking_findings"]:
            out("  BLOCKING FINDINGS:\n")
            for finding in summary["blocking_findings"]:
                out(f"    - {finding['kind']}: {finding['detail']}\n")
    for lane in report.lanes:
        marker = "ok" if lane.available else "UNAVAILABLE"
        out(f"lane {lane.name}: {marker} -- {lane.detail}\n")
    for problem in report.problems:
        out(f"problem: {problem}\n")
    out(f"project state  {report.state}\n")


def _write_bundle(out_dir: Path, report: RunReport, compiled: CompiledProject) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run-report.json").write_text(json.dumps(report.as_body(), indent=2, default=str))
    (out_dir / "compiler-output-manifest.json").write_text(
        json.dumps(compiled.manifest.body if compiled.manifest else {}, indent=2, default=str)
    )
    evidence = compiled.gate_evidence()
    for name, body in evidence.items():
        (out_dir / f"{name.replace('_', '-')}.json").write_text(json.dumps(body, indent=2, default=str))
    if compiled.recompilation is not None:
        (out_dir / "amendment-001-recompilation.json").write_text(
            json.dumps(
                {
                    "step_6": compiled.recompilation.step_6_body(),
                    "step_7": compiled.recompilation.step_7_body(),
                    "delivery_priority": compiled.recompilation.delivery_priority.as_body(),
                },
                indent=2,
                default=str,
            )
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pack_root = Path(args.pack)

    if args.action == "validate":
        try:
            pack = load_pack(pack_root)
        except PackValidationError as exc:
            sys.stderr.write(f"FAILED_CONTRACT: {exc}\n")
            return EXIT_CODES[ProjectState.FAILED_CONTRACT]
        body = {
            "project_id": pack.project_id,
            "contract_id": pack.contract_id,
            "contract_version": pack.contract_version,
            "manifest_hash": pack.manifest_hash,
            "files": pack.file_manifest(),
            "gates": sorted(pack.acceptance_gates()),
            "oracles": sorted(pack.oracle_definitions()),
        }
        sys.stdout.write(json.dumps(body, indent=2) if args.json else f"pack valid: {pack.manifest_hash}\n")
        return 0

    report = run_project(pack_root, mode=args.mode, start_workflow=args.action == "run")
    compiled = report.project

    if args.json:
        sys.stdout.write(json.dumps(report.as_body(), indent=2, default=str) + "\n")
    else:
        _print_human(report, compiled)

    if args.out and compiled is not None:
        _write_bundle(Path(args.out), report, compiled)

    return EXIT_CODES.get(report.state, 1)


if __name__ == "__main__":
    raise SystemExit(main())
