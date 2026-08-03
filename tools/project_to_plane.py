#!/usr/bin/env python3
"""Push the compiled project into Plane, so the owner can see it without reading a chat log.

**Why this exists.** `integrations/plane.py` is complete -- a real client with
``upsert``, ``comment``, ``link`` and ``health``, a ``PlaneProjection`` with
``build_payload`` and ``write_back``, measured against the live API on
2026-08-02. ``api/deps.py:19`` even carries the intended wiring in a docstring::

    projection=PlaneProjection.from_pack(...),   # this workstream

and the parameter beside it defaults to ``None``. Nothing in ``src/`` ever
constructs one. The walking skeleton's station 4 reports "projection adapter
constructed", which is true and is not the same as projecting anything.

So the component whose entire job is to let the owner see project state has
never once shown them project state. That is §26's "modules built but not
wired" landing on the one module whose absence is felt rather than measured --
and the owner said so directly: *"i dont have a dashboard to know what is going
where"*.

**What it projects.** The compiled contract is the authoritative source: 57
tasks, their milestones, workstreams, phases, requirement links and the
11-task critical path, joined to live gate verdicts so a task gated by a
failing gate does not read as healthy. Section 4.1 fixes Plane at
``mode: projection_only``, so this is one-way -- TerminusDB and the compiler
are truth, Plane is a view. Nothing is ever read back from Plane into a
decision.

**Idempotent by external id.** ``PlaneClient.upsert`` finds by
``find_by_external_id`` first, so re-running updates rather than duplicating.
The 409-on-duplicate behaviour recorded in ``checks_d2_11`` is the API telling
us the same thing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from api.state import ControlPlaneSnapshot, ProjectRecord, TaskRecord  # noqa: E402
from contracts.compiler import compile_pack  # noqa: E402
from governance.envelope import utc_now  # noqa: E402
from governance.states import ProjectState, TaskState  # noqa: E402
from integrations.pack import load_pack  # noqa: E402
from integrations.plane import PlaneProjection  # noqa: E402


def _task_state(raw: str | None, gate_verdicts: dict[str, str], gate_ids: list[str]) -> TaskState:
    """The compiled state, unless a gate this task depends on has spoken.

    A task whose gate FAILs is not PROPOSED any more, and showing it as
    PROPOSED is how a dashboard becomes decorative. Where the gates disagree the
    worst verdict wins -- an optimistic join is the same failure in a different
    hat.
    """
    verdicts = {gate_verdicts.get(g) for g in gate_ids} - {None}
    if "FAIL" in verdicts:
        # Each gate YAML declares its own ``failure_state``; every gate that can
        # fail today declares FAILED_ORACLE, so that is what a failing gate puts
        # on the task. If a gate ever declares a different one this must read it
        # from the gate rather than assume -- flagged here so it is a decision
        # someone makes, not a default nobody noticed.
        return TaskState.FAILED_ORACLE
    try:
        return TaskState(str(raw))
    except ValueError:
        return TaskState.PROPOSED


def build_snapshot(pack_root: Path) -> ControlPlaneSnapshot:
    """Compile the pack, join live gate verdicts, and render one consistent read."""
    pack = load_pack(pack_root)
    compiled = compile_pack(pack)

    gate_verdicts: dict[str, str] = {}
    try:
        from evaluation.gate_runner import GateRunner

        summary = GateRunner().run()
        payload = summary.as_dict() if hasattr(summary, "as_dict") else summary
        for gate in payload.get("gates") or []:
            gate_verdicts[str(gate.get("gate_id"))] = str(gate.get("verdict"))
    except Exception as exc:
        print(f"  gate verdicts unavailable ({type(exc).__name__}); projecting compiled state only",
              file=sys.stderr)

    critical = set(getattr(compiled.critical_path, "nodes", ()) or ())
    tasks: list[TaskRecord] = []
    for task_id in sorted(compiled.tasks):
        t = compiled.tasks[task_id]
        gate_ids = list(t.get("gate_ids") or ())
        tasks.append(
            TaskRecord(
                task_id=task_id,
                project_id=pack.project_id,
                title=str(t.get("title") or task_id),
                state=_task_state(t.get("state"), gate_verdicts, gate_ids),
                milestone_id=t.get("milestone_id"),
                workstream=t.get("workstream_id"),
                phase=t.get("phase"),
                depends_on=tuple(t.get("depends_on") or ()),
                requirement_ids=tuple(t.get("requirement_ids") or ()),
                allowed_paths=tuple(t.get("allowed_paths") or ()),
                on_critical_path=task_id in critical,
            )
        )

    project = ProjectRecord(
        project_id=pack.project_id,
        name=str(getattr(pack, "project_name", None) or pack.project_id),
        state=ProjectState.RUNNING,
        contract_id=compiled.contract_id,
        contract_version=compiled.contract_version,
        pack_manifest_hash=getattr(pack, "manifest_hash", None),
    )
    return ControlPlaneSnapshot(project=project, tasks=tuple(tasks), captured_at=utc_now())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-root", type=Path, default=REPO_ROOT / "project-pack")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and render the payload; contact Plane only for health")
    args = parser.parse_args()

    snapshot = build_snapshot(args.pack_root)
    on_path = sum(1 for t in snapshot.tasks if t.on_critical_path)
    print(f"snapshot: {len(snapshot.tasks)} tasks, {on_path} on the critical path")
    by_state: dict[str, int] = {}
    for t in snapshot.tasks:
        by_state[str(t.state)] = by_state.get(str(t.state), 0) + 1
    for state, n in sorted(by_state.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {state}")

    projection = PlaneProjection.from_pack(load_pack(args.pack_root))
    if not projection.is_available():
        print("Plane is not available (no credential, or the host did not answer).", file=sys.stderr)
        return 2

    if args.dry_run:
        payload = projection.build_payload(snapshot)
        items = payload.get("work_items") if isinstance(payload, dict) else payload
        print(f"dry run: would upsert {len(items or [])} work item(s); nothing sent")
        return 0

    result = projection.project(snapshot)
    print(f"projected: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
