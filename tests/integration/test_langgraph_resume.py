"""GATE-D1-04 -- resume from checkpoint without restarting completed work.

Contract Section 10.6:

    Successful parallel nodes MUST not be rerun when another node fails if their
    outputs were checkpointed and remain valid.

Two independent probes, because they prove different halves of the claim:

1. :func:`test_gate_d1_04_process_kill_then_resume_does_not_rerun_completed_nodes`
   -- a real ``SIGKILL`` of a real child process mid-super-step, then a fresh
   process that resumes. Nothing survives in memory between the two, so the
   evidence for "did not re-run" is a filesystem side-effect counter written by
   the node body itself. A mock cannot fake this: the counter is incremented
   before anything else the node does.
2. :func:`test_real_task_graph_resumes_without_rerunning_completed_nodes` -- the
   registered ``task_graph`` from Section 10.2, interrupted by a classified
   failure and resumed through :class:`WorkflowRuntime`, proving the same
   property holds for the graphs the harness actually ships.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from assignments.leases import InMemoryLeaseLedger
from governance.states import FailureClass, TaskState
from workflows.checkpoint import SqliteCheckpointAdapter
from workflows.failures import ClassifiedFailure
from workflows.graphs import WorkflowServices
from workflows.runtime import WorkflowRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "project-pack"
SRC = REPO_ROOT / "src"

THREAD_ID = "GATE-D1-04"

# ---------------------------------------------------------------------------
# Probe 1: kill the process, resume in a new one.
# ---------------------------------------------------------------------------

CHILD_SOURCE = '''
"""Child process for the GATE-D1-04 kill/restart probe.

Runs a two-branch graph on the real Section 10.3 adapter. ``durable_branch``
finishes immediately; ``hanging_branch`` blocks until the parent kills the
process. Every node records its execution to its own append-only file *before*
doing anything else, so the counter cannot be faked by a later write.
"""
import asyncio, json, sys, time
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from workflows.checkpoint import SqliteCheckpointAdapter
from workflows.state import WorkflowState, initial_state

D = Path(sys.argv[1])
MODE = sys.argv[2]


def record(name):
    with open(D / ("count." + name), "a") as fh:
        fh.write("x")
        fh.flush()


def durable_branch(state):
    record("durable_branch")
    (D / "durable_branch.done").write_text("1")
    return {"output_hashes": {"probe.durable": "sha256:durable"}, "node_log": ["durable_branch"]}


def hanging_branch(state):
    record("hanging_branch")
    if MODE == "kill":
        deadline = time.time() + 120
        while time.time() < deadline:
            if (D / "sibling.persisted").exists():
                break
            time.sleep(0.02)
        time.sleep(120)   # the parent SIGKILLs us in here
    return {"output_hashes": {"probe.hanging": "sha256:hanging"}, "node_log": ["hanging_branch"]}


def join(state):
    record("join")
    return {"node_log": ["join"]}


async def main():
    builder = StateGraph(WorkflowState)
    builder.add_node("durable_branch", durable_branch)
    builder.add_node("hanging_branch", hanging_branch)
    builder.add_node("join", join)
    builder.add_edge(START, "durable_branch")
    builder.add_edge(START, "hanging_branch")
    builder.add_edge("durable_branch", "join")
    builder.add_edge("hanging_branch", "join")
    builder.add_edge("join", END)

    async with SqliteCheckpointAdapter.open(D / "checkpoints.sqlite") as adapter:
        graph = builder.compile(checkpointer=adapter.saver())
        config = {"configurable": {"thread_id": "%(thread_id)s"}}
        state = None
        if MODE == "kill":
            state = initial_state(
                project_id="EFAH-001",
                project_version="1.1",
                contract_version="1.1",
                terminus_database="efah",
                terminus_branch="main",
                terminus_commit="probe-commit",
                work_unit_id="WU-0001",
                graph_id="resume_probe",
            )
        result = await graph.ainvoke(state, config)
        (D / "result.json").write_text(json.dumps({
            "node_log": result.get("node_log", []),
            "output_hashes": result.get("output_hashes", {}),
        }))


asyncio.run(main())
''' % {"thread_id": THREAD_ID}


def _count(directory: Path, node: str) -> int:
    path = directory / f"count.{node}"
    return len(path.read_bytes()) if path.exists() else 0


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


async def _wait_for(predicate, *, timeout: float, interval: float = 0.05) -> bool:
    """Poll an async predicate until it is true or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _durable_write_landed(store: Path) -> bool:
    """True once the finished sibling's write is in the checkpoint store.

    Read through the adapter, not by poking sqlite: if the adapter cannot
    observe durability, "the output was checkpointed" is not a claim this lane
    can make.
    """
    if not store.exists():
        return False
    try:
        async with SqliteCheckpointAdapter.open(store) as adapter:
            return "output_hashes" in await adapter.pending_write_channels(THREAD_ID)
    except Exception:  # noqa: BLE001 -- concurrent writer; retry on the next tick
        return False


async def test_gate_d1_04_process_kill_then_resume_does_not_rerun_completed_nodes(tmp_path: Path):
    script = tmp_path / "resume_probe_child.py"
    script.write_text(CHILD_SOURCE)
    store = tmp_path / "checkpoints.sqlite"

    child = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, str(script), str(tmp_path), "kill"],
        env=_child_env(),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    async def fast_branch_ran() -> bool:
        return (tmp_path / "durable_branch.done").exists()

    try:
        assert await _wait_for(fast_branch_ran, timeout=60), "the fast branch never ran"

        # Wait until the completed branch's write is *durable*, so the kill
        # tests resume-without-rerun rather than a race we happened to win.
        async def durable_write_landed() -> bool:
            return await _durable_write_landed(store)

        landed = await _wait_for(durable_write_landed, timeout=60)
        assert landed, "the completed node's write never reached the checkpoint store"

        (tmp_path / "sibling.persisted").write_text("1")
        await asyncio.sleep(0.3)

        assert child.poll() is None, "the child exited before it could be killed"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover -- cleanup path
            child.kill()
            child.wait(timeout=10)

    assert child.returncode == -signal.SIGKILL
    assert _count(tmp_path, "durable_branch") == 1
    assert _count(tmp_path, "hanging_branch") == 1
    assert _count(tmp_path, "join") == 0

    # A brand-new process, no shared memory, no input: resume from checkpoint.
    resumed = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path), "resume"],
        env=_child_env(),
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=120,
    )
    assert resumed.returncode == 0, resumed.stderr.decode()[-4000:]

    # GATE-D1-04 A2: completed_node_rerun_count == 0.
    assert _count(tmp_path, "durable_branch") == 1, "a completed node was re-executed on resume"
    # The node that was killed mid-flight had to run again; the join ran once.
    assert _count(tmp_path, "hanging_branch") == 2
    assert _count(tmp_path, "join") == 1

    # GATE-D1-04 A1: the run continued rather than restarting.
    result = json.loads((tmp_path / "result.json").read_text())
    assert set(result["output_hashes"]) >= {"probe.durable", "probe.hanging"}
    assert result["node_log"].count("durable_branch") == 1
    assert "join" in result["node_log"]

    # A3: the surviving checkpoints still satisfy Section 10.4.
    async with SqliteCheckpointAdapter.open(store) as adapter:
        dump = await adapter.field_dump(THREAD_ID)
    assert dump
    assert all(entry["terminus_commit"] == "probe-commit" for entry in dump)


# ---------------------------------------------------------------------------
# Probe 2: the same property, on a graph the harness actually ships.
# ---------------------------------------------------------------------------


async def test_real_task_graph_resumes_without_rerunning_completed_nodes(tmp_path: Path):
    """``task_graph`` (Section 10.2) survives a classified failure and resumes.

    ``claim_lease`` acquires a real lease. ``execute_work_unit`` fails once with
    a classified ``TRANSIENT_PROVIDER_FAILURE``. The runtime classifies, decides
    to resume, and the resumed run must not re-acquire the lease -- a second
    ``claim_lease`` would supersede the first generation and turn the worker's
    own submission stale.
    """
    executions: list[str] = []
    fail_once = {"pending": True}

    def observer(graph_id: str, node_name: str) -> None:
        executions.append(f"{graph_id}:{node_name}")
        if node_name == "execute_work_unit" and fail_once["pending"]:
            fail_once["pending"] = False
            raise ClassifiedFailure(FailureClass.TRANSIENT_PROVIDER_FAILURE, "provider 503 mid-run")

    services = WorkflowServices(
        pack_root=PACK_ROOT,
        ledger=InMemoryLeaseLedger(),
        worktree_root=str(tmp_path / "worktrees"),
        node_observer=observer,
    )

    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="task_graph", work_unit_id="WU-0001")
        attempts = await runtime.run_with_recovery("task_graph", state, thread_id="WU-0001")

    assert len(attempts) == 2, [a.error for a in attempts]

    first, second = attempts
    assert first.completed is False
    # Section 10.6: classify before retrying.
    assert first.failure_class is FailureClass.TRANSIENT_PROVIDER_FAILURE
    assert first.retry is not None and first.retry.retry is True
    assert first.retry.owner_interrupt_required is False

    assert second.resumed is True
    assert second.completed is True, second.error

    # The completed node ran exactly once across both attempts.
    assert executions.count("task_graph:claim_lease") == 1
    assert executions.count("task_graph:execute_work_unit") == 2
    assert executions.count("task_graph:submit_candidate") == 1

    # One lease, one generation -- the resume did not re-claim the work unit.
    leases = services.ledger.active_leases()
    assert len(leases) == 1
    assert leases[0].generation == 1
    assert second.state["lease_generation"] == 1

    # And the resumed run still ends in a lawful worker state.
    assert second.state["artifacts"]["task_state"] == str(TaskState.CANDIDATE_COMPLETE)


async def test_a_never_retry_failure_is_not_resumed(tmp_path: Path):
    """Counter-control: recovery is classified, not automatic.

    ``CONTRACT_DRIFT`` is in ``NEVER_RETRY``. Re-running work the contract has
    already invalidated is not recovery, so ``run_with_recovery`` must stop at
    one attempt rather than loop.
    """

    def observer(graph_id: str, node_name: str) -> None:
        if node_name == "claim_lease":
            raise ClassifiedFailure(FailureClass.CONTRACT_DRIFT, "contract version moved under the run")

    services = WorkflowServices(
        pack_root=PACK_ROOT,
        ledger=InMemoryLeaseLedger(),
        worktree_root=str(tmp_path / "worktrees"),
        node_observer=observer,
    )
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="task_graph", work_unit_id="WU-0001")
        attempts = await runtime.run_with_recovery("task_graph", state, thread_id="WU-0001")

    assert len(attempts) == 1
    assert attempts[0].failure_class is FailureClass.CONTRACT_DRIFT
    assert attempts[0].retry is not None and attempts[0].retry.retry is False
