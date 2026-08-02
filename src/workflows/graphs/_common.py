"""Shared construction machinery for the twelve required graphs (Section 10.2).

Two things live here rather than being repeated twelve times:

* :class:`WorkflowServices` -- the injected collaborators. They are *not* put in
  ``config["configurable"]``, because anything in the config is a candidate for
  the checkpoint, and Section 10.3 pins the checkpointer to strict safe
  serialization. A lease ledger is not serialisable state; it is a service.
  Binding services at build time keeps the checkpoint made of plain data.
* :func:`node` -- the wrapper every graph node goes through, so that
  ``graph_node`` (Section 10.4) and the append-only ``node_log`` are maintained
  by the framework rather than by each author remembering to.

``node_observer`` is the seam the resume probe uses to count executions. It
observes; it never substitutes for the real node.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from assignments.leases import InMemoryLeaseLedger, LeaseLedger
from integrations.pack import ProjectPack, load_pack
from workflows.state import WorkflowState

NodeFn = Callable[[WorkflowState], dict[str, Any]]


@dataclass
class TerminusBinding:
    """Section 10.4 requires database, branch, and commit on every checkpoint.

    WS-B owns the real TerminusDB adapter. The runtime carries the binding it
    was given and refuses to invent one -- an empty commit id would satisfy a
    field check while destroying the provenance the field exists for.
    """

    database: str = "efah"
    branch: str = "main"
    commit: str = "uncommitted"


@dataclass
class WorkflowServices:
    """Everything the graphs need that is not graph state."""

    pack_root: Path
    ledger: LeaseLedger = field(default_factory=InMemoryLeaseLedger)
    terminus: TerminusBinding = field(default_factory=TerminusBinding)
    #: Called as ``observer(graph_id, node_name)`` immediately before each node
    #: body runs. Used by the GATE-D1-04 resume probe to count re-executions.
    node_observer: Callable[[str, str], None] | None = None
    #: Bound on work units per build run, so an integration test that exercises
    #: the real path does not have to run all twenty-six of them.
    max_work_units: int = 3
    #: Section 9.5 branch/worktree ownership. These are ownership *identifiers*
    #: recorded on the lease; creating the worktree is the worker's job, not the
    #: runtime's, and WS-A owns that lane.
    worktree_root: str = ".worktrees"
    default_role: str = "implementer"
    #: Section 11 keeps real model identity out of the runtime. The runtime only
    #: ever sees the blinded alias, and never resolves it.
    default_blinded_alias: str = "MODEL-A"
    _pack: ProjectPack | None = field(default=None, repr=False)

    @property
    def pack(self) -> ProjectPack:
        if self._pack is None:
            self._pack = load_pack(self.pack_root)
        return self._pack


def node(graph_id: str, name: str, services: WorkflowServices) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node body so Section 10.4 bookkeeping cannot be forgotten."""

    def decorate(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        def wrapper(state: WorkflowState) -> dict[str, Any]:
            if services.node_observer is not None:
                services.node_observer(graph_id, name)
            result = dict(fn(state) or {})
            result.setdefault("graph_node", name)
            result.setdefault("graph_id", graph_id)
            result["node_log"] = [f"{graph_id}:{name}", *result.get("node_log", [])]
            return result

        wrapper.__name__ = name
        return wrapper

    return decorate
