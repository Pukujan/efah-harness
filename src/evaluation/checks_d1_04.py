"""GATE-D1-04 — LangGraph resumes from checkpoint without restarting work.

Contract Sections 10.3, 10.4 and 10.6. Four of the gate's five assertions are
executed here against the real Section 10.3 adapter, the real Section 10.2
``task_graph``, and a real process that is really killed:

    A1 the process is killed mid-run and the project resumes without reset
    A2 nodes that completed before the kill are not re-executed
    A3 every checkpoint references all twelve Section 10.4 fields
    A5 the checkpointer is reached only through the adapter

A4 is **not** registered. It asks whether deleting the checkpoint store leaves
project truth intact in TerminusDB, and the second half of that question needs a
live TerminusDB to answer. Deleting the store and then asserting that the store
is not authoritative would be a check on this module's own opinion, so the
assertion is left to report ``NOT_IMPLEMENTED`` with its reason rather than pass
on the half that is easy.

Three rules shaped the checks that *are* here.

**A kill has to be a kill.** A1 delegates to
``tests/integration/test_langgraph_resume.py``, which starts a child process,
waits until a finished sibling's write is durable in the store, sends a real
``SIGKILL``, and then resumes in a brand-new process. Nothing survives in memory
across that boundary, which is the whole point: a probe that "restarts" inside
one interpreter proves that an object was reused, not that a run resumed. The
delegation follows the pattern ``evaluation.checks._d1_07`` established -- the
pinned suite decides, and this check reports what it decided.

**The counter must be written by the node, before the node does anything else.**
A2 drives that same child probe itself, because the evidence GATE-D1-04 names is
``node_execution_counters`` and a count is only worth reading if it survived the
kill. Each node appends a byte to its own file as its first act, so the file
length is an execution count no later write can retouch. The decisive line of
the whole gate is then one comparison: the resumed process emitted
``probe.durable`` in its output while ``count.durable_branch`` stayed at 1. An
output that appears without its producer running can only have come from the
checkpoint.

**A check that cannot fail is not a check.** Every assertion below carries a
negative control that makes the property false and requires the same detector to
fire:

* A1 hands a fresh process no input and no checkpoint store -- it must refuse
  and execute nothing, so the positive arm's continuation is attributable to the
  store rather than to the script;
* A2 restarts the run on a clean thread instead of resuming it, which is exactly
  the defect the assertion forbids, and the same counter must report a rerun;
* A3 writes a checkpoint with one Section 10.4 field removed, and once more with
  it set to ``None``, and the adapter must refuse both at write time;
* A5 runs the import scanner over the adapter module itself with the exemption
  lifted, which must flag it -- otherwise "zero offenders" would be a statement
  about a scanner that finds nothing anywhere.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from assignments.leases import InMemoryLeaseLedger
from evaluation.async_bridge import run_sync
from evaluation.gate_spec import AssertionSpec, GateSpec
from governance.envelope import content_hash
from governance.states import FailureClass
from workflows.checkpoint import SqliteCheckpointAdapter
from workflows.failures import ClassifiedFailure
from workflows.graphs import WorkflowServices, build
from workflows.runtime import WorkflowRuntime
from workflows.state import (
    REQUIRED_CHECKPOINT_FIELDS,
    CheckpointReference,
    MissingCheckpointFields,
    missing_required_fields,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; ``ok`` and ``bad`` are the only runtime needs, and resolving them
# on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


#: The pinned probe. A1 delegates its verdict to this file's tests; A2 borrows
#: its child-process source and its helpers, so both assertions measure the same
#: subject rather than two lookalike reimplementations of it.
PROBE_PATH = Path("tests") / "integration" / "test_langgraph_resume.py"

#: The two tests that decide A1, each with the claim it carries -- a selector
#: nobody can read is a delegation nobody can audit. The third test in that file
#: is a counter-control about NEVER_RETRY classes and belongs to Section 10.6,
#: not to this assertion.
A1_TEST_CLAIMS: dict[str, str] = {
    "test_gate_d1_04_process_kill_then_resume_does_not_rerun_completed_nodes": (
        "a real child process is SIGKILLed after a finished sibling's write is durable in the "
        "store, and a brand-new process resumes the thread with no input"
    ),
    "test_real_task_graph_resumes_without_rerunning_completed_nodes": (
        "the registered Section 10.2 task_graph survives a classified failure and is resumed by "
        "WorkflowRuntime without re-claiming its lease"
    ),
}
A1_TESTS: tuple[str, ...] = tuple(A1_TEST_CLAIMS)

#: Nodes the kill/restart child records. ``durable_branch`` is the one that
#: finished before the kill, so it is the node A2's count is about.
PROBE_NODES: tuple[str, ...] = ("durable_branch", "hanging_branch", "join")
COMPLETED_BEFORE_KILL = "durable_branch"
#: The output channel that node wrote. Its presence after a resume that did not
#: re-run the node is the proof that the value came out of the checkpoint.
DURABLE_OUTPUT_KEY = "probe.durable"

#: The Section 10.2 graph the in-process arms exercise, and a work unit that
#: exists in the compiled plan (``claim_lease`` refuses one that does not).
GRAPH_ID = "task_graph"
WORK_UNIT_ID = "WU-0001"
INTERRUPT_AT = "execute_work_unit"

_SUBPROCESS_TIMEOUT_SECONDS = 900


# ===========================================================================
# Shared probe machinery
# ===========================================================================


@contextmanager
def _workspace(prefix: str) -> Iterator[Path]:
    """A scratch directory that is removed however the check exits.

    Probes here write sqlite stores, worktree roots and counter files. Leaving
    them behind would let one run's checkpoint answer the next run's question.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _probe_module(repo_root: Path) -> ModuleType:
    """Load the pinned resume probe as a module, without pytest.

    A2 needs ``CHILD_SOURCE`` and the polling helpers, not the assertions. This
    is the same technique ``evaluation.checks._load_d1_07_module`` uses for the
    pinned GATE-D1-07 tool: the file is the single definition of the probe, and
    a copy of it in this module would be a second definition free to drift.
    """
    path = repo_root / PROBE_PATH
    spec = importlib.util.spec_from_file_location("_gate_d1_04_resume_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the pinned resume probe at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _services(repo_root: Path, worktree_root: Path, observer: Any = None) -> WorkflowServices:
    """Real services with a real ledger. Only the observer is injected."""
    return WorkflowServices(
        pack_root=repo_root / "project-pack",
        ledger=InMemoryLeaseLedger(),
        worktree_root=str(worktree_root),
        node_observer=observer,
    )


def _pytest_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _tail(proc: subprocess.CompletedProcess[str], limit: int = 12) -> list[str]:
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return [line for line in text.strip().splitlines() if line.strip()][-limit:]


# ===========================================================================
# A1 — the process is killed mid-run and the project resumes without reset
# ===========================================================================


def _resume_without_a_store(module: ModuleType, repo_root: Path, workdir: Path) -> dict[str, Any]:
    """Negative control: a fresh process, no input, and no checkpoint store.

    The positive arm's second process is handed exactly the same command line as
    this one. The only difference is that a store exists for it to continue
    from. If this arm also ran the graph, "the run continues" would be a
    statement about the script rather than about the checkpoint.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "resume_probe_child.py"
    script.write_text(module.CHILD_SOURCE)
    proc = subprocess.run(
        [sys.executable, str(script), str(workdir), "resume"],
        env=module._child_env(),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    counts = {node: module._count(workdir, node) for node in PROBE_NODES}
    stderr_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
    return {
        "probe": "resume the same child in a directory with no checkpoint store",
        "why": (
            "the resumed process in the positive arm is given no input either. Unless it is "
            "the store that lets it continue, 'the run continues' describes the script and "
            "not the checkpoint."
        ),
        "returncode": proc.returncode,
        "refused": proc.returncode != 0,
        "nodes_executed": counts,
        "produced_a_result": (workdir / "result.json").exists(),
        "detector_fires": proc.returncode != 0 and sum(counts.values()) == 0,
        "error": stderr_lines[-1] if stderr_lines else "",
    }


def d1_04_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``kill_and_restart_probe`` -- expected ``project_state_preserved and run_continues``.

    The verdict is the pinned suite's. Both selected tests must pass: the first
    kills a real child with ``SIGKILL`` after waiting for a finished sibling's
    write to become durable, then resumes in a new process; the second does the
    same thing to the registered ``task_graph`` through :class:`WorkflowRuntime`,
    so the property is shown on a graph the harness actually ships and not only
    on a probe built to demonstrate it.

    A subprocess rather than an in-process call because the probe kills processes
    and manipulates signals; a check that did that inside the gate runner would
    be deciding one assertion at the cost of the run.
    """
    probe_path = ctx.repo_root / PROBE_PATH
    if not probe_path.is_file():
        return bad([f"the pinned resume probe is missing: {PROBE_PATH.as_posix()}"])

    selectors = [f"{PROBE_PATH.as_posix()}::{name}" for name in A1_TESTS]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *selectors, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ctx.repo_root),
        env=_pytest_env(),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    passed = proc.returncode == 0
    tail = _tail(proc)

    module = _probe_module(ctx.repo_root)
    with _workspace("gate-d1-04-a1-") as workroot:
        control = _resume_without_a_store(module, ctx.repo_root, workroot / "no-store")

    transcript = {
        "check": a.method or "kill_and_restart_probe",
        "expected": a.expected,
        "delegated_to": PROBE_PATH.as_posix(),
        "selectors": selectors,
        "what_each_selector_proves": {
            name: A1_TEST_CLAIMS.get(name, "no claim is recorded for this selector")
            for name in A1_TESTS
        },
        "signal_used": f"SIGKILL ({int(signal.SIGKILL)}) -- not a cooperative shutdown",
        "returncode": proc.returncode,
        "passed": passed,
        "detail": tail,
        "negative_control": control,
        "honest_limit": (
            "the verdict is the pinned suite's. This check does not re-derive the probe's "
            "assertions; it selects them by name, runs them in a subprocess, and reports what "
            "they decided."
        ),
    }
    evidence = {
        "kill_restart_transcript": transcript,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "probe_source_hash": content_hash(probe_path.read_bytes()),
            "transcript_hash": content_hash(transcript),
        },
    }

    findings: list[str] = []
    if not passed:
        findings.append(f"the kill/restart probe failed (rc={proc.returncode})")
        findings.extend(tail[-8:])
    if not control["detector_fires"]:
        findings.append(
            "negative control did not fire: a fresh process with no checkpoint store still "
            f"made progress ({control['nodes_executed']}, rc={control['returncode']}), so a "
            "passing resume would not be attributable to the store"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{len(selectors)} pinned probes pass: a SIGKILLed run resumes in a new process and "
            "the shipped task_graph resumes after a classified failure. The same resume command "
            "with no store refuses outright and executes nothing"
        ),
    )


# ===========================================================================
# A2 — nodes that completed before the kill are not re-executed
# ===========================================================================


async def _kill_and_resume_counts(
    module: ModuleType, repo_root: Path, workdir: Path
) -> dict[str, Any]:
    """Drive the pinned child: run it, kill it, resume it, read the counters.

    Every helper used here comes from the probe module, so the sequence is the
    one the integration suite performs rather than a lookalike. What this
    function adds is the bookkeeping GATE-D1-04 asks for as evidence: the
    counters on both sides of the kill, the durability observation taken *before*
    the kill, and the resumed process's own output.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "resume_probe_child.py"
    script.write_text(module.CHILD_SOURCE)
    store = workdir / "checkpoints.sqlite"
    thread_id = module.THREAD_ID

    child = subprocess.Popen(
        [sys.executable, str(script), str(workdir), "kill"],
        env=module._child_env(),
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    record: dict[str, Any] = {"thread_id": thread_id}
    try:

        async def fast_branch_ran() -> bool:
            return (workdir / "durable_branch.done").exists()

        async def durable_write_landed() -> bool:
            return await module._durable_write_landed(store)

        record["completed_node_ran"] = await module._wait_for(fast_branch_ran, timeout=60)
        record["write_became_durable_before_the_kill"] = await module._wait_for(
            durable_write_landed, timeout=60
        )
        async with SqliteCheckpointAdapter.open(store) as adapter:
            latest = await adapter.latest(thread_id)
            record["pending_write_channels_before_the_kill"] = sorted(
                await adapter.pending_write_channels(thread_id)
            )
            record["pending_write_tasks_before_the_kill"] = len(
                {task for task, _channel in (latest.pending_writes if latest else ())}
            )
        (workdir / "sibling.persisted").write_text("1")
        await asyncio.sleep(0.3)
        record["child_alive_at_the_moment_of_the_kill"] = child.poll() is None
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover - cleanup path
            child.kill()
            child.wait(timeout=10)

    record["child_returncode"] = child.returncode
    record["killed_by_sigkill"] = child.returncode == -signal.SIGKILL
    record["counts_after_the_kill"] = {node: module._count(workdir, node) for node in PROBE_NODES}

    resumed = subprocess.run(
        [sys.executable, str(script), str(workdir), "resume"],
        env=module._child_env(),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    record["resume_returncode"] = resumed.returncode
    record["resume_error"] = ((resumed.stderr or "").strip().splitlines() or [""])[-1]
    record["counts_after_the_resume"] = {node: module._count(workdir, node) for node in PROBE_NODES}
    result_path = workdir / "result.json"
    record["resumed_result"] = json.loads(result_path.read_text()) if result_path.exists() else None
    async with SqliteCheckpointAdapter.open(store) as adapter:
        record["state_carrying_checkpoints"] = len(await adapter.field_dump(thread_id))
    return record


async def _observer_rerun_probe(
    repo_root: Path, workdir: Path, *, restart_instead_of_resume: bool
) -> dict[str, Any]:
    """Count node executions across a failure on the shipped ``task_graph``.

    ``WorkflowServices.node_observer`` is called immediately before each node
    body, so the list it builds is an execution log and not a summary somebody
    computed afterwards. The observer also injects the failure, which keeps the
    interruption inside the real node-dispatch path rather than around it.

    ``restart_instead_of_resume`` is the negative control: same graph, same
    adapter, same counter, but the second attempt starts a clean thread from
    fresh input. That is precisely the defect A2 forbids, and it must show up as
    a non-zero rerun count.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    executions: list[str] = []
    fail_once = {"pending": True}

    def observer(graph_id: str, node_name: str) -> None:
        executions.append(node_name)
        if node_name == INTERRUPT_AT and fail_once["pending"]:
            fail_once["pending"] = False
            raise ClassifiedFailure(
                FailureClass.TRANSIENT_PROVIDER_FAILURE, "provider 503 mid-run (GATE-D1-04 probe)"
            )

    services = _services(repo_root, workdir / "worktrees", observer)
    async with SqliteCheckpointAdapter.open(workdir / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id=GRAPH_ID, work_unit_id=WORK_UNIT_ID)
        first = await runtime.run(GRAPH_ID, state, thread_id="T-D1-04")
        first_nodes = list(executions)
        checkpoint = await adapter.latest("T-D1-04")

        if restart_instead_of_resume:
            # A fresh ledger, because a restart is a run that knows nothing of
            # the first one -- including its lease.
            restart_services = _services(repo_root, workdir / "worktrees-restart", observer)
            restart_runtime = WorkflowRuntime(restart_services, adapter)
            second = await restart_runtime.run(
                GRAPH_ID,
                restart_runtime.new_state(graph_id=GRAPH_ID, work_unit_id=WORK_UNIT_ID),
                thread_id="T-D1-04-RESTART",
            )
            ledger = restart_services.ledger
        else:
            second = await runtime.resume(GRAPH_ID, thread_id="T-D1-04")
            ledger = services.ledger

    second_nodes = executions[len(first_nodes):]
    # The node that raised is the last one the first attempt entered; every node
    # before it finished, and those are the nodes this assertion is about.
    interrupted = first_nodes[-1] if first_nodes else ""
    completed_before = [node for node in first_nodes if node != interrupted]
    rerun_count = sum(second_nodes.count(node) for node in completed_before)
    leases = ledger.active_leases()

    return {
        "graph_id": GRAPH_ID,
        "mode": "restart on a clean thread" if restart_instead_of_resume else "resume the thread",
        "first_attempt_nodes": first_nodes,
        "interrupted_node": interrupted,
        "completed_before_the_interruption": completed_before,
        "second_attempt_nodes": second_nodes,
        "completed_node_rerun_count": rerun_count,
        "first_attempt_completed": first.completed,
        "first_attempt_failure_class": first.failure_class.value if first.failure_class else None,
        "second_attempt_completed": second.completed,
        "second_attempt_resumed": second.resumed,
        "second_attempt_error": second.error,
        "checkpoint_carried_the_completed_node_output": bool(
            checkpoint is not None and checkpoint.channel_values.get("lease_generation")
        ),
        "active_leases": len(leases),
        "lease_generations": [lease.generation for lease in leases],
    }


def d1_04_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A2 ``node_execution_counter_compare`` -- expected ``completed_node_rerun_count == 0``.

    Two independent counters, because each is weak where the other is strong.

    The filesystem counter survives a ``SIGKILL``: the child's nodes append a
    byte to their own file before doing anything else, so nothing in the killed
    process could have tidied the record afterwards. What it cannot do is
    distinguish "the node did not run" from "the node ran and produced nothing",
    which is why the decisive comparison is the resumed process's *output*:
    ``probe.durable`` appears in the result while ``count.durable_branch`` is
    still 1. A value cannot be produced by a node that did not execute, so it
    came from the checkpoint.

    The observer counter runs on the shipped ``task_graph`` and sees node names
    rather than files, so it also catches the case a filesystem probe cannot --
    a resumed run that re-claims the lease and quietly supersedes the worker's
    own generation. Section 9.5 makes that a stale-assignment bug, and the lease
    generation is asserted here for that reason.

    The negative control restarts instead of resuming. Same graph, same adapter,
    same counter, and the rerun count must be non-zero -- otherwise the counter
    is not measuring re-execution at all.
    """
    module = _probe_module(ctx.repo_root)
    with _workspace("gate-d1-04-a2-") as workroot:
        killed = run_sync(_kill_and_resume_counts(module, ctx.repo_root, workroot / "kill"))
        resumed = run_sync(
            _observer_rerun_probe(
                ctx.repo_root, workroot / "resume", restart_instead_of_resume=False
            )
        )
        restarted = run_sync(
            _observer_rerun_probe(
                ctx.repo_root, workroot / "restart", restart_instead_of_resume=True
            )
        )

    before = killed["counts_after_the_kill"]
    after = killed["counts_after_the_resume"]
    filesystem_rerun_count = after[COMPLETED_BEFORE_KILL] - before[COMPLETED_BEFORE_KILL]
    result = killed["resumed_result"] or {}
    output_hashes = result.get("output_hashes") or {}
    durable_output_survived = DURABLE_OUTPUT_KEY in output_hashes

    counters = {
        "check": a.method or "node_execution_counter_compare",
        "expected": a.expected,
        "process_kill_arm": {
            **killed,
            "completed_node": COMPLETED_BEFORE_KILL,
            "completed_node_rerun_count": filesystem_rerun_count,
            "output_of_the_completed_node_survived_without_re_execution": durable_output_survived,
            "how_the_count_is_taken": (
                "each node appends one byte to count.<node> as its first statement, so the file "
                "length is an execution count written before the node could do anything else and "
                "unreachable to the process after SIGKILL"
            ),
            "what_the_durability_observation_does_and_does_not_show": (
                "pending_write_channels names the channels durably written against the current "
                "checkpoint but not which task wrote them, so it alone does not separate the "
                "input super-step's writes from the finished node's. The decisive evidence is "
                f"that the resumed process emitted {DURABLE_OUTPUT_KEY!r} while "
                f"count.{COMPLETED_BEFORE_KILL} stayed at {after[COMPLETED_BEFORE_KILL]}"
            ),
        },
        "shipped_graph_arm": resumed,
        "negative_control": {
            "probe": "second attempt starts a clean thread from fresh input instead of resuming",
            "why": (
                "'zero reruns' is satisfied for free by a counter that never increments. This "
                "arm makes the property false in the one way the assertion names and requires "
                "the same counter to report it."
            ),
            **restarted,
            "detector_fires": restarted["completed_node_rerun_count"] > 0,
        },
    }
    evidence = {
        "node_execution_counters": counters,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(counters),
        },
    }

    findings: list[str] = []
    if not killed["completed_node_ran"]:
        findings.append("the child's fast branch never ran, so there is no completed node to count")
    if not killed["write_became_durable_before_the_kill"]:
        findings.append(
            "the completed node's write never reached the checkpoint store before the kill; "
            "Section 10.6 only protects outputs that were checkpointed"
        )
    if not killed["killed_by_sigkill"]:
        findings.append(
            f"the child exited with {killed['child_returncode']} rather than being SIGKILLed, "
            "so nothing was interrupted"
        )
    if killed["resume_returncode"] != 0:
        findings.append(f"the resumed process failed: {killed['resume_error']}")
    if filesystem_rerun_count != 0:
        findings.append(
            f"{COMPLETED_BEFORE_KILL} was executed {filesystem_rerun_count} more time(s) after "
            f"the resume: {before} -> {after}"
        )
    if not durable_output_survived:
        findings.append(
            f"the resumed run did not carry {DURABLE_OUTPUT_KEY!r} forward "
            f"({sorted(output_hashes)}), so the completed node's output was lost rather than "
            "restored from the checkpoint"
        )
    if resumed["completed_node_rerun_count"] != 0:
        findings.append(
            f"on the shipped {GRAPH_ID}, {resumed['completed_before_the_interruption']} ran again "
            f"after the resume ({resumed['second_attempt_nodes']})"
        )
    if not resumed["second_attempt_completed"]:
        findings.append(
            f"the resumed {GRAPH_ID} did not complete: {resumed['second_attempt_error']}"
        )
    if resumed["lease_generations"] != [1]:
        findings.append(
            f"the resumed run left lease generations {resumed['lease_generations']}; a re-claimed "
            "lease supersedes the worker's own generation and makes its submission stale (§9.5)"
        )
    if restarted["completed_node_rerun_count"] <= 0:
        findings.append(
            "negative control did not fire: a run restarted on a clean thread reported "
            f"{restarted['completed_node_rerun_count']} reruns, so the counter is not measuring "
            "re-execution"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"across a real SIGKILL, {COMPLETED_BEFORE_KILL} executed exactly "
            f"{after[COMPLETED_BEFORE_KILL]} time and its output {DURABLE_OUTPUT_KEY!r} still "
            f"reached the resumed result; on the shipped {GRAPH_ID} the completed node ran once "
            "and the lease stayed at generation 1. A restart in place of a resume is counted as "
            f"{restarted['completed_node_rerun_count']} rerun(s)"
        ),
    )


# ===========================================================================
# A3 — every checkpoint references all twelve Section 10.4 fields
# ===========================================================================


async def _checkpoint_field_probe(repo_root: Path, workdir: Path) -> dict[str, Any]:
    """Run the shipped graph for real, then try to write two illegal checkpoints.

    Positive and negative arms share one adapter and one graph builder, so the
    control cannot pass through a different code path than the verdict. The two
    controls differ in the one way Section 10.4 says matters: a field that is
    absent and a field that is present but ``None``. A checkpoint recording
    ``terminus_commit: null`` would satisfy a key-presence check while destroying
    the provenance binding the field exists for.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    async with SqliteCheckpointAdapter.open(workdir / "checkpoints.sqlite") as adapter:

        async def invoke(thread_id: str, mutate: Any = None) -> None:
            # A ledger per arm: ``claim_lease`` refuses a work unit that is
            # already leased, and that refusal has nothing to do with A3.
            services = _services(repo_root, workdir / f"worktrees-{thread_id}")
            runtime = WorkflowRuntime(services, adapter)
            state = dict(runtime.new_state(graph_id=GRAPH_ID, work_unit_id=WORK_UNIT_ID))
            if mutate is not None:
                mutate(state)
            graph = build(GRAPH_ID, services).compile(checkpointer=adapter.saver())
            await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}})

        await invoke("T-A3")
        records = await adapter.list_checkpoints("T-A3")
        dump = await adapter.field_dump("T-A3")

        controls: dict[str, Any] = {}
        mutations: tuple[tuple[str, Any], ...] = (
            ("field_absent", lambda state: state.pop("terminus_commit")),
            ("field_present_but_null", lambda state: state.__setitem__("terminus_commit", None)),
        )
        for label, mutate in mutations:
            thread_id = f"T-A3-{label}"
            entry: dict[str, Any] = {"victim_field": "terminus_commit"}
            try:
                await invoke(thread_id, mutate)
            except Exception as exc:
                # The raised type *is* the measurement: only a Section 10.4
                # refusal means this control fired for the reason it claims.
                cause = _refused_for_missing_fields(exc)
                entry["refused"] = cause is not None
                entry["raised"] = type(exc).__name__
                entry["missing"] = list(cause.missing) if cause is not None else []
                entry["detail"] = str(exc)[:400]
            else:
                entry["refused"] = False
                entry["raised"] = None
                entry["missing"] = []
                entry["detail"] = "the incomplete checkpoint was accepted"
            written = await adapter.list_checkpoints(thread_id)
            entry["state_carrying_checkpoints_written"] = sum(1 for r in written if r.carries_state)
            controls[label] = entry

    return {
        "records": [
            {
                "step": record.step,
                "checkpoint_id": record.checkpoint_id,
                "carries_graph_state": record.carries_state,
                "missing_required_fields": missing_required_fields(record.channel_values),
                "next_nodes": list(record.next_nodes),
            }
            for record in records
        ],
        "dump": dump,
        "controls": controls,
    }


def _refused_for_missing_fields(exc: BaseException) -> MissingCheckpointFields | None:
    """Unwrap to the Section 10.4 refusal, if that is what happened.

    LangGraph re-raises a node failure wrapped in its own context, and "some
    exception was raised" is not the same claim as "the write was refused for
    the field this control removed". Only the latter makes the control fire for
    the right reason.
    """
    seen: list[int] = []
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, MissingCheckpointFields):
            return cause
        seen.append(id(cause))
        cause = cause.__cause__ or cause.__context__
    return None


def d1_04_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``checkpoint_schema_assert`` -- expected ``all_of checkpoint_references present``.

    "Twelve" is checked before anything else, and against two sources: the closed
    tuple in :mod:`workflows.state` and the typed
    :class:`~workflows.state.CheckpointReference`. If those disagree, or if the
    tuple stops having twelve entries, the field dump below would be a complete
    dump of an incomplete list -- a green for a weaker rule than the contract's.

    The dump itself comes from a real run of the shipped ``task_graph``, and
    every state-carrying checkpoint is re-examined directly rather than trusting
    ``field_dump``, which filters to the records it considers complete. A filter
    that hid an incomplete checkpoint would produce a flawless dump of a subset.

    The controls are the load-bearing half. Section 10.4 compliance is not
    achieved by a well-behaved caller here: ``_Section104EnforcingSaver.aput``
    refuses the write. Both controls must be refused at write time, refused for
    the field they removed, and must leave no state-carrying checkpoint behind.
    """
    declared = list(REQUIRED_CHECKPOINT_FIELDS)
    model_fields = sorted(CheckpointReference.model_fields)

    with _workspace("gate-d1-04-a3-") as workroot:
        probe = run_sync(_checkpoint_field_probe(ctx.repo_root, workroot))

    state_carrying = [r for r in probe["records"] if r["carries_graph_state"]]
    incomplete = [r for r in state_carrying if r["missing_required_fields"]]
    dump = probe["dump"]
    dump_gaps = [
        {
            "index": index,
            "missing": sorted(set(declared) - set(entry)),
            "null": sorted(key for key, value in entry.items() if value is None),
        }
        for index, entry in enumerate(dump)
        if set(declared) - set(entry) or any(value is None for value in entry.values())
    ]

    field_dump = {
        "check": a.method or "checkpoint_schema_assert",
        "expected": a.expected,
        "graph_id": GRAPH_ID,
        "required_fields": declared,
        "required_field_count": len(declared),
        "typed_model_fields": model_fields,
        "tuple_and_model_agree": sorted(declared) == model_fields,
        "checkpoints_written": len(probe["records"]),
        "state_carrying_checkpoints": len(state_carrying),
        "checkpoints_missing_a_required_field": incomplete,
        "records": probe["records"],
        "dump": dump,
        "dump_entries_with_gaps": dump_gaps,
        "enforced_at": (
            "workflows.checkpoint._Section104EnforcingSaver.aput -- the assertion runs before the "
            "write, so an incomplete checkpoint cannot exist to be found later"
        ),
        "negative_control": {
            "probe": (
                "write a checkpoint with terminus_commit removed, and again with it set to None, "
                "through the same graph and the same adapter"
            ),
            "why": (
                "'every checkpoint carries all twelve fields' is trivially true of a run that "
                "writes no checkpoints, and equally true of a saver that never checks. These "
                "arms make the field absent and then null, and the write must be refused."
            ),
            **probe["controls"],
            "detector_fires": all(entry["refused"] for entry in probe["controls"].values()),
        },
    }
    evidence = {
        "checkpoint_field_dump": field_dump,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(field_dump),
        },
    }

    findings: list[str] = []
    if len(declared) != 12:
        findings.append(
            "Section 10.4 names twelve checkpoint references; REQUIRED_CHECKPOINT_FIELDS has "
            f"{len(declared)}: {declared}"
        )
    if sorted(declared) != model_fields:
        findings.append(
            "the enforced field tuple and the typed CheckpointReference disagree: "
            f"{sorted(declared)} vs {model_fields}"
        )
    if not state_carrying:
        findings.append(
            "the run produced no state-carrying checkpoint, so 'every checkpoint carries all "
            "twelve fields' is vacuous"
        )
    if not dump:
        findings.append("the field dump is empty; there is no evidence to inspect")
    findings.extend(
        f"checkpoint at step {record['step']} omits {record['missing_required_fields']}"
        for record in incomplete
    )
    findings.extend(
        f"field dump entry {gap['index']} is missing {gap['missing']} and nulls {gap['null']}"
        for gap in dump_gaps
    )
    if len(dump) != len(state_carrying):
        findings.append(
            f"{len(state_carrying)} state-carrying checkpoints but {len(dump)} dump entries; "
            "the dump is filtering out records rather than reporting them"
        )
    for label, entry in probe["controls"].items():
        if not entry["refused"]:
            findings.append(
                f"negative control {label} did not fire: {entry['detail']} "
                f"(raised {entry['raised']})"
            )
        elif "terminus_commit" not in entry["missing"]:
            findings.append(
                f"negative control {label} was refused but not for the field it removed: "
                f"missing={entry['missing']}"
            )
        if entry["state_carrying_checkpoints_written"]:
            findings.append(
                f"negative control {label} still persisted "
                f"{entry['state_carrying_checkpoints_written']} state-carrying checkpoint(s)"
            )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{len(dump)} state-carrying checkpoints from a real {GRAPH_ID} run each reference all "
            f"{len(declared)} Section 10.4 fields, and the adapter refuses a write with any one of "
            "them absent or null"
        ),
    )


# ===========================================================================
# A5 — the checkpointer is reached only through the adapter
# ===========================================================================

#: Section 10.3 names ``AsyncSqliteSaver``; nothing outside the adapter may name
#: it, or any other saver, or the serializer the adapter configures strictly.
#: Ported from ``tests/unit/test_workflow_checkpoint.py`` so the gate decides the
#: same question the unit suite does, from the same roots and symbols.
CHECKPOINTER_IMPORT_ROOTS: tuple[str, ...] = ("langgraph.checkpoint",)
CHECKPOINTER_SYMBOLS: tuple[str, ...] = (
    "AsyncSqliteSaver",
    "SqliteSaver",
    "InMemorySaver",
    "JsonPlusSerializer",
)


def _imported_names(path: Path) -> set[str]:
    """Every module and symbol name a file imports, by AST rather than by text.

    A regular expression over source would match the word in a docstring, and
    ``workflows/checkpoint.py`` explains itself at length. Parsing means the scan
    sees imports and only imports.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _reaches_the_checkpointer(names: set[str]) -> list[str]:
    """The predicate. Both arms of A5 run this one function."""
    hits = {
        name
        for name in names
        if any(name == root or name.startswith(root + ".") for root in CHECKPOINTER_IMPORT_ROOTS)
        or name.rsplit(".", 1)[-1] in CHECKPOINTER_SYMBOLS
    }
    return sorted(hits)


def d1_04_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A5 ``import_boundary_test`` -- expected ``zero_direct_asyncsqlitesaver_imports_outside_adapter``.

    The scan is over ``src/``: every Python file is parsed and its import names
    are matched against the checkpointer roots and symbols. One file is exempt --
    ``workflows/checkpoint.py``, which Section 10.3 designates as the adapter.

    The negative control lifts that exemption and scans the adapter itself. It
    must be flagged. Without that arm, "zero offenders across N files" would be
    equally true of a scanner that matched nothing at all, and a boundary check
    that cannot see the one import it exists to police is not policing anything.
    """
    src = ctx.repo_root / "src"
    adapter_module = src / "workflows" / "checkpoint.py"
    if not adapter_module.is_file():
        return bad([f"the Section 10.3 adapter is missing: {adapter_module}"])

    offenders: list[dict[str, Any]] = []
    unparseable: list[str] = []
    scanned = 0
    for path in sorted(src.rglob("*.py")):
        if path == adapter_module:
            continue
        try:
            names = _imported_names(path)
        except SyntaxError as exc:
            unparseable.append(f"{path.relative_to(src).as_posix()}: {exc}")
            continue
        scanned += 1
        hits = _reaches_the_checkpointer(names)
        if hits:
            offenders.append({"path": path.relative_to(src).as_posix(), "imports": hits})

    control_hits = _reaches_the_checkpointer(_imported_names(adapter_module))

    report = {
        "check": a.method or "import_boundary_test",
        "expected": a.expected,
        "scan_root": "src",
        "files_scanned": scanned,
        "exempt": adapter_module.relative_to(ctx.repo_root).as_posix(),
        "import_roots": list(CHECKPOINTER_IMPORT_ROOTS),
        "symbols": list(CHECKPOINTER_SYMBOLS),
        "offenders": offenders,
        "files_that_would_not_parse": unparseable,
        "how_the_scan_reads_a_file": (
            "ast.walk over Import and ImportFrom nodes. A textual search would match the "
            "adapter's own prose about AsyncSqliteSaver, and every module that documents the "
            "boundary would look like a violation of it."
        ),
        "negative_control": {
            "probe": "scan workflows/checkpoint.py with its exemption lifted",
            "why": (
                "zero offenders is exactly what a scanner that matches nothing reports. The one "
                "file that is supposed to import the checkpointer must be flagged when it is not "
                "exempt, or the scan proves nothing about the files that are."
            ),
            "imports_found": control_hits,
            "detector_fires": bool(control_hits),
        },
    }
    evidence = {
        "import_boundary_report": report,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "adapter_source_hash": content_hash(adapter_module.read_bytes()),
            "transcript_hash": content_hash(report),
        },
    }

    findings: list[str] = []
    findings.extend(
        f"{entry['path']} reaches the checkpointer directly: {entry['imports']}"
        for entry in offenders
    )
    findings.extend(
        f"a source file could not be parsed, so it went unscanned: {entry}" for entry in unparseable
    )
    if scanned == 0:
        findings.append("no source files were scanned, so 'zero offenders' is vacuous")
    if not control_hits:
        findings.append(
            "negative control did not fire: the adapter itself shows no checkpointer import, so "
            "the scanner would report zero offenders whatever the other files did"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{scanned} files under src/ parsed and none imports the checkpointer; the exempt "
            f"adapter is flagged by the same scanner ({control_hits}) when the exemption is lifted"
        ),
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
#:
#: A4 is deliberately absent. ``delete_checkpoints_then_query_terminusdb``
#: expects ``project_and_task_state_still_authoritative_in_terminusdb``, and the
#: query half needs a live TerminusDB. The adapter side is easy -- ``destroy()``
#: exists and ``is_authoritative`` is ``False`` -- and stopping there would
#: report that project truth survived without ever asking the system that holds
#: it. That is the shape of a green that measured the adjacent property.
CHECKS_D1_04: dict[tuple[str, str], Check] = {
    ("GATE-D1-04", "A1"): d1_04_a1,
    ("GATE-D1-04", "A2"): d1_04_a2,
    ("GATE-D1-04", "A3"): d1_04_a3,
    ("GATE-D1-04", "A5"): d1_04_a5,
}
