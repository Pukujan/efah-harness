"""The walking skeleton, end to end, on the real checkpoint store.

Contract Sections 10.2, 10.3, 10.4. No mocks: the real project pack, the real
``AsyncSqliteSaver`` behind the Section 10.3 adapter, the real lease ledger, and
the real ORACLE-002 fencing gate on every candidate submission.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assignments.leases import InMemoryLeaseLedger
from governance.states import TaskState, Verdict
from planning.decomposition import decompose, plan_hash
from workflows.checkpoint import SqliteCheckpointAdapter
from workflows.graphs import REQUIRED_GRAPHS, WorkflowServices, compile_all
from workflows.runtime import WorkflowRuntime
from workflows.state import REQUIRED_CHECKPOINT_FIELDS

PACK_ROOT = Path(__file__).resolve().parents[2] / "project-pack"


@pytest.fixture
def services(tmp_path: Path) -> WorkflowServices:
    return WorkflowServices(
        pack_root=PACK_ROOT,
        ledger=InMemoryLeaseLedger(),
        worktree_root=str(tmp_path / "worktrees"),
        max_work_units=3,
    )


async def test_project_graph_runs_end_to_end_against_a_real_sqlite_checkpointer(
    services: WorkflowServices, tmp_path: Path
):
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="project_graph", work_unit_id="WU-0001")
        outcome = await runtime.run("project_graph", state, thread_id="skeleton")

        assert outcome.completed, outcome.error
        assert outcome.state is not None

        log = outcome.state["node_log"]
        # Every stage of the skeleton actually executed, in contract order.
        for stage in ("intake", "research", "contract", "planning", "build", "evaluation", "deployment", "closeout"):
            assert f"project_graph:{stage}" in log
        assert log.index("project_graph:intake") < log.index("project_graph:closeout")

        # The three walking-skeleton graphs did real work.
        assert "intake_graph:load_pack" in log
        assert "planning_graph:compile_work_units" in log
        assert log.count("task_graph:submit_candidate") == services.max_work_units

        # ... and produced real, hashed outputs rather than empty state.
        assert outcome.state["input_hashes"]["pack.manifest"].startswith("sha256:")
        assert any(k.startswith("candidate.") for k in outcome.state["output_hashes"])


async def test_every_work_unit_passes_the_lease_fencing_gate(
    services: WorkflowServices, tmp_path: Path
):
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="project_graph", work_unit_id="WU-0001")
        outcome = await runtime.run("project_graph", state, thread_id="skeleton")

    outcomes = outcome.state["artifacts"]["work_unit_outcomes"]
    assert len(outcomes) == services.max_work_units
    for work_unit_id, result in outcomes.items():
        assert result["oracle_002"] == str(Verdict.PASS), work_unit_id
        assert result["task_state"] == str(TaskState.CANDIDATE_COMPLETE), work_unit_id

    # Section 9.3: a worker never produces PASSED.
    assert str(TaskState.PASSED) not in str(outcomes)

    # Section 9.5: one lease per work unit, all distinct, all distinct worktrees.
    leases = services.ledger.active_leases()
    assert len({lease.lease_id for lease in leases}) == len(leases) == services.max_work_units
    assert len({lease.worktree for lease in leases}) == len(leases)


async def test_gate_d1_04_a3_every_state_carrying_checkpoint_holds_all_twelve_fields(
    services: WorkflowServices, tmp_path: Path
):
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="project_graph", work_unit_id="WU-0001")
        await runtime.run("project_graph", state, thread_id="skeleton")

        records = await adapter.list_checkpoints("skeleton")
        stateful = [r for r in records if r.carries_state]
        assert len(stateful) > 10, "the run produced almost no durable checkpoints"

        for record in stateful:
            reference = record.references().model_dump()
            assert set(reference) == set(REQUIRED_CHECKPOINT_FIELDS)
            assert reference["project_id"] == "EFAH-001"
            assert reference["terminus_commit"]

        dump = await adapter.field_dump("skeleton")
        assert len(dump) == len(stateful)


async def test_gate_d1_04_a4_deleting_the_checkpoint_store_does_not_destroy_project_truth(
    services: WorkflowServices, tmp_path: Path
):
    """Section 10.1: LangGraph holds execution state; truth lives elsewhere.

    WS-B owns the TerminusDB authority check. What is provable from inside this
    lane is the other half of the same claim: everything the checkpoint held is
    re-derivable from the pack and the assignment ledger, so deleting the store
    costs progress and nothing else.
    """
    path = tmp_path / "checkpoints.sqlite"
    async with SqliteCheckpointAdapter.open(path) as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="project_graph", work_unit_id="WU-0001")
        outcome = await runtime.run("project_graph", state, thread_id="skeleton")
        before = await adapter.list_checkpoints("skeleton")
        assert before

    plan_before = outcome.state["output_hashes"]["planning.plan"]
    lease_events_before = len(services.ledger.events())

    await adapter.destroy()
    assert not path.exists()

    # Project truth: the compiled plan is a pure function of the pack.
    assert plan_hash(decompose(services.pack)) == plan_before
    # Assignment truth: the lease ledger is untouched by the deletion.
    assert len(services.ledger.events()) == lease_events_before
    assert services.ledger.active_leases()

    # And the store rebuilds itself on the next run.
    async with SqliteCheckpointAdapter.open(path) as adapter:
        assert await adapter.list_checkpoints("skeleton") == []


async def test_all_twelve_graphs_compile_against_the_real_checkpointer(
    services: WorkflowServices, tmp_path: Path
):
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        compiled = compile_all(services, checkpointer=adapter.saver())
    assert set(compiled) == set(REQUIRED_GRAPHS)


async def test_task_graph_runs_standalone_on_its_own_thread(
    services: WorkflowServices, tmp_path: Path
):
    """``task_graph`` is reachable on its own, not only inside ``build_graph``."""
    async with SqliteCheckpointAdapter.open(tmp_path / "checkpoints.sqlite") as adapter:
        runtime = WorkflowRuntime(services, adapter)
        state = runtime.new_state(graph_id="task_graph", work_unit_id="WU-0007")
        outcome = await runtime.run("task_graph", state, thread_id="WU-0007")

    assert outcome.completed, outcome.error
    assert outcome.state["artifacts"]["task_state"] == str(TaskState.CANDIDATE_COMPLETE)
    assert outcome.state["gate_verdicts"]["ORACLE-002"]["verdict"] == str(Verdict.PASS)
    assert outcome.state["lease_generation"] == 1
