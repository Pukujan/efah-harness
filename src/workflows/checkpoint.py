"""Checkpoint adapter -- the ONLY module that may touch a LangGraph saver.

Contract EFAH-CONTRACT-001 v1.1 Section 10.3:

    The deadline build SHALL use ``AsyncSqliteSaver`` as the initial LangGraph
    checkpointer when running on one host. It MUST be hidden behind a checkpoint
    adapter, use strict safe serialization configuration, and be treated as
    rebuildable execution state.

Three consequences are implemented here, not merely documented:

1. **Hidden.** Nothing outside this module imports ``langgraph.checkpoint``.
   ``tests/unit/test_workflow_checkpoint_boundary.py`` scans ``src/`` and fails
   the build otherwise (GATE-D1-04 A5).
2. **Strict safe serialization.** ``JsonPlusSerializer`` defaults to a
   *permissive* msgpack allowlist: any type may be revived, which means an
   attacker who can write to the checkpoint file can trigger code execution on
   read. We construct it with ``pickle_fallback=False`` and an explicit
   allowlist (``allowed_msgpack_modules=None`` -> only LangGraph's built-in safe
   types), and refuse to let a caller widen it silently.
3. **Rebuildable, not authoritative.** ``is_authoritative`` is ``False`` and
   :meth:`SqliteCheckpointAdapter.destroy` exists precisely so a test can delete
   the store and prove project truth survives (GATE-D1-04 A4). Replacing this
   adapter with another officially supported durable checkpointer must not touch
   a single domain schema -- that is why the return types below are plain
   dataclasses rather than LangGraph's ``CheckpointTuple``.

DEC-001 is binding: LangGraph is the permanent runtime. Temporal is a contract
non-goal and is never a checkpoint backend.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from workflows.state import CheckpointReference, assert_checkpoint_fields, missing_required_fields

#: Contract Section 10.3 default location for the single-host deadline build.
DEFAULT_CHECKPOINT_PATH = Path("./.data/checkpoints.sqlite")


class CheckpointAdapterError(RuntimeError):
    """Adapter-level failure. Never leaks a vendor exception type upward."""


@dataclass(frozen=True)
class CheckpointRecord:
    """Vendor-neutral view of one checkpoint.

    Deliberately not ``CheckpointTuple``: swapping the backing checkpointer
    (Section 10.3 allows it) must not change what callers see.
    """

    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    step: int
    channel_values: dict[str, Any]
    pending_writes: tuple[tuple[str, str], ...] = ()
    next_nodes: tuple[str, ...] = ()

    @property
    def carries_state(self) -> bool:
        return not missing_required_fields(self.channel_values)

    def references(self) -> CheckpointReference:
        """Section 10.4 field dump -- GATE-D1-04 ``checkpoint_field_dump``."""
        return CheckpointReference.from_channel_values(self.channel_values)


@runtime_checkable
class CheckpointAdapter(Protocol):
    """The seam Section 10.3 requires between the runtime and its store."""

    #: Section 10.3: checkpoints are rebuildable execution state, never truth.
    is_authoritative: bool

    def saver(self) -> Any:
        """Return the object a compiled graph is given as its checkpointer."""

    async def list_checkpoints(self, thread_id: str) -> list[CheckpointRecord]: ...

    async def latest(self, thread_id: str) -> CheckpointRecord | None: ...

    async def destroy(self) -> None: ...


def strict_serializer(
    *,
    extra_allowed_modules: Iterable[tuple[str, ...] | type] = (),
) -> JsonPlusSerializer:
    """Section 10.3 "strict safe serialization configuration".

    ``pickle_fallback=False`` refuses to serialise anything msgpack cannot
    describe rather than embedding a pickle payload. ``allowed_msgpack_modules``
    and ``allowed_json_modules`` are pinned to ``None`` so deserialisation is
    restricted to LangGraph's built-in safe-type allowlist; the harness state
    model is plain ``str``/``int``/``list``/``dict``, so it needs no widening.
    ``extra_allowed_modules`` exists for a future state model that genuinely
    needs a custom type, and is additive-only -- there is no way to reach the
    permissive default through this function.
    """
    serde = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    extras = tuple(extra_allowed_modules)
    if extras:
        serde = serde.with_msgpack_allowlist(extras)
    return serde


class _Section104EnforcingSaver(AsyncSqliteSaver):
    """``AsyncSqliteSaver`` that refuses a checkpoint missing Section 10.4 fields.

    Enforcing on write rather than asserting after the fact is the difference
    between a gate that can be satisfied by a well-behaved caller and one that
    cannot be violated. GATE-D1-04 A3 asserts *every* checkpoint carries all
    twelve references; the only way to make that true is to make the alternative
    impossible.
    """

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        assert_checkpoint_fields(dict(checkpoint.get("channel_values") or {}))
        return await super().aput(config, checkpoint, metadata, new_versions)


@dataclass
class SqliteCheckpointAdapter:
    """Section 10.3 initial checkpoint profile, behind the adapter seam.

    Usage::

        async with SqliteCheckpointAdapter.open(path) as adapter:
            graph = builder.compile(checkpointer=adapter.saver())
    """

    path: Path
    _conn: aiosqlite.Connection | None = field(default=None, repr=False)
    _saver: _Section104EnforcingSaver | None = field(default=None, repr=False)

    #: Section 10.3 / GATE-D1-04 A4. The store is rebuildable execution state.
    is_authoritative: bool = False
    rebuildable: bool = True

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        path: Path | str = DEFAULT_CHECKPOINT_PATH,
        *,
        extra_allowed_modules: Iterable[tuple[str, ...] | type] = (),
    ) -> AsyncIterator["SqliteCheckpointAdapter"]:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        adapter = cls(path=target)
        async with aiosqlite.connect(str(target)) as conn:
            adapter._conn = conn
            adapter._saver = _Section104EnforcingSaver(
                conn, serde=strict_serializer(extra_allowed_modules=extra_allowed_modules)
            )
            try:
                yield adapter
            finally:
                adapter._saver = None
                adapter._conn = None

    # -- the seam ----------------------------------------------------------

    def saver(self) -> BaseCheckpointSaver:
        if self._saver is None:
            raise CheckpointAdapterError("adapter is not open; use `async with SqliteCheckpointAdapter.open(...)`")
        return self._saver

    @property
    def serializer_profile(self) -> dict[str, Any]:
        """Evidence that Section 10.3's serialization clause actually holds."""
        serde = self.saver().serde
        return {
            "serializer": type(serde).__name__,
            "pickle_fallback": getattr(serde, "pickle_fallback", None),
            "allowed_json_modules": getattr(serde, "_allowed_json_modules", None),
            "permissive_msgpack": getattr(serde, "_allowed_msgpack_modules", None) is True,
        }

    # -- read side ---------------------------------------------------------

    async def list_checkpoints(self, thread_id: str) -> list[CheckpointRecord]:
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        out: list[CheckpointRecord] = []
        async for tup in self.saver().alist(cfg):
            out.append(_to_record(tup))
        return out

    async def latest(self, thread_id: str) -> CheckpointRecord | None:
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        tup = await self.saver().aget_tuple(cfg)
        return _to_record(tup) if tup is not None else None

    async def pending_write_channels(self, thread_id: str) -> tuple[str, ...]:
        """Channels already durably written by tasks that finished this step.

        Section 10.6: "Successful parallel nodes MUST not be rerun when another
        node fails if their outputs were checkpointed." This is how a caller
        observes that a node's output *is* durable -- used by the resume probe
        so the kill happens after, not before, the write landed.
        """
        record = await self.latest(thread_id)
        if record is None:
            return ()
        return tuple(channel for _task, channel in record.pending_writes)

    async def field_dump(self, thread_id: str) -> list[dict[str, Any]]:
        """GATE-D1-04 ``checkpoint_field_dump`` evidence for one thread."""
        return [
            record.references().model_dump()
            for record in await self.list_checkpoints(thread_id)
            if record.carries_state
        ]

    # -- lifecycle ---------------------------------------------------------

    async def delete_thread(self, thread_id: str) -> None:
        await self.saver().adelete_thread(thread_id)

    async def destroy(self) -> None:
        """Delete the store. Legal by construction -- it is not project truth."""
        if self._conn is not None:
            raise CheckpointAdapterError("close the adapter before destroying the store")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                os.unlink(candidate)


def _to_record(tup: Any) -> CheckpointRecord:
    cfg = tup.config.get("configurable", {})
    parent_cfg = (tup.parent_config or {}).get("configurable", {})
    metadata = tup.metadata or {}
    return CheckpointRecord(
        thread_id=cfg.get("thread_id", ""),
        checkpoint_ns=cfg.get("checkpoint_ns", ""),
        checkpoint_id=cfg.get("checkpoint_id", ""),
        parent_checkpoint_id=parent_cfg.get("checkpoint_id"),
        step=int(metadata.get("step", -1)),
        channel_values=dict(tup.checkpoint.get("channel_values") or {}),
        pending_writes=tuple((task_id, channel) for task_id, channel, _value in (tup.pending_writes or [])),
        next_nodes=tuple(_next_nodes(tup)),
    )


def _next_nodes(tup: Any) -> Sequence[str]:
    tasks = (tup.metadata or {}).get("writes")
    if isinstance(tasks, dict):
        return tuple(tasks)
    return ()
