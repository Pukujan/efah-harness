"""Contract Section 10.3 -- the checkpoint adapter, and the seam it defends.

Covers GATE-D1-04 A3 (every checkpoint carries the Section 10.4 references) and
A5 (the checkpointer is reached only through the adapter).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from langgraph.graph import END, START, StateGraph

from workflows.checkpoint import (
    DEFAULT_CHECKPOINT_PATH,
    CheckpointAdapterError,
    SqliteCheckpointAdapter,
    strict_serializer,
)
from workflows.state import MissingCheckpointFields, WorkflowState, initial_state

SRC = Path(__file__).resolve().parents[2] / "src"
ADAPTER_MODULE = SRC / "workflows" / "checkpoint.py"

#: Section 10.3 names AsyncSqliteSaver; nothing else may name it.
CHECKPOINTER_IMPORT_ROOTS = ("langgraph.checkpoint",)
CHECKPOINTER_SYMBOLS = ("AsyncSqliteSaver", "SqliteSaver", "InMemorySaver", "JsonPlusSerializer")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def test_gate_d1_04_a5_no_direct_checkpointer_import_outside_the_adapter():
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == ADAPTER_MODULE:
            continue
        names = _imports(path)
        if any(n.startswith(root) for n in names for root in CHECKPOINTER_IMPORT_ROOTS):
            offenders.append(str(path.relative_to(SRC)))
        elif any(n.rsplit(".", 1)[-1] in CHECKPOINTER_SYMBOLS for n in names):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"checkpointer reached outside the Section 10.3 adapter: {offenders}"


def test_default_path_is_the_contract_specified_location():
    assert DEFAULT_CHECKPOINT_PATH == Path("./.data/checkpoints.sqlite")


def test_strict_serializer_refuses_the_permissive_default():
    """Section 10.3 "strict safe serialization configuration"."""
    serde = strict_serializer()
    assert serde.pickle_fallback is False
    # ``True`` is LangGraph's permissive allowlist: any type may be revived.
    assert serde._allowed_msgpack_modules is not True
    assert serde._allowed_json_modules is not True


def test_extra_allowlist_is_additive_and_cannot_reopen_the_permissive_default():
    serde = strict_serializer(extra_allowed_modules=[("workflows", "state")])
    assert serde._allowed_msgpack_modules is not True


async def test_adapter_reports_its_serialization_profile(tmp_path: Path):
    async with SqliteCheckpointAdapter.open(tmp_path / "cp.sqlite") as adapter:
        profile = adapter.serializer_profile
    assert profile["pickle_fallback"] is False
    assert profile["permissive_msgpack"] is False


async def test_adapter_is_not_authoritative(tmp_path: Path):
    async with SqliteCheckpointAdapter.open(tmp_path / "cp.sqlite") as adapter:
        assert adapter.is_authoritative is False
        assert adapter.rebuildable is True


async def test_saver_is_unavailable_outside_the_context(tmp_path: Path):
    async with SqliteCheckpointAdapter.open(tmp_path / "cp.sqlite") as adapter:
        adapter.saver()
    with pytest.raises(CheckpointAdapterError):
        adapter.saver()


async def test_gate_d1_04_a3_checkpoint_missing_a_required_field_is_refused(tmp_path: Path):
    """Negative control: the adapter refuses a Section 10.4-incomplete write.

    The graph below deliberately omits ``terminus_commit``. Without enforcement
    it would checkpoint happily and the provenance binding would be missing.
    """
    builder: StateGraph = StateGraph(WorkflowState)

    def only_partial_state(state: WorkflowState) -> dict:
        return {"node_log": ["partial"]}

    builder.add_node("only_partial_state", only_partial_state)
    builder.add_edge(START, "only_partial_state")
    builder.add_edge("only_partial_state", END)

    incomplete = dict(
        initial_state(
            project_id="EFAH-001",
            project_version="1.1",
            contract_version="1.1",
            terminus_database="efah",
            terminus_branch="main",
            terminus_commit="abc123",
            work_unit_id="WU-0001",
            graph_id="probe",
        )
    )
    del incomplete["terminus_commit"]

    async with SqliteCheckpointAdapter.open(tmp_path / "cp.sqlite") as adapter:
        graph = builder.compile(checkpointer=adapter.saver())
        with pytest.raises(MissingCheckpointFields) as excinfo:
            await graph.ainvoke(incomplete, {"configurable": {"thread_id": "T-partial"}})
    assert "terminus_commit" in excinfo.value.missing


async def test_destroy_removes_the_store_and_requires_a_closed_adapter(tmp_path: Path):
    path = tmp_path / "cp.sqlite"
    async with SqliteCheckpointAdapter.open(path) as adapter:
        assert path.exists()
        with pytest.raises(CheckpointAdapterError):
            await adapter.destroy()
    await adapter.destroy()
    assert not path.exists()
