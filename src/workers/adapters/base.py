"""The worker adapter port.

Contract Section 5.1: everything external sits behind an adapter. GATE-D1-07
A3/A5: a Claude adapter, if it exists at all, is *one optional implementation of
this port*, and removing it must leave a working one behind.

Nothing in this module may import a vendor SDK, and nothing outside
``src/workers/adapters/`` may either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from governance.envelope import utc_now
from governance.states import TaskState
from models.router import RoutingDecision
from workers.session import WorkUnit


@dataclass
class WorkerOutcome:
    """Contract Section 9.4 work-unit result, plus Section 18 provenance.

    Carries an alias, never a vendor. ``configuration_hash``, ``input_hash`` and
    ``output_hash`` are what make the run replayable and the record checkable.
    """

    task_id: str
    role: str
    alias: str
    gateway: str
    adapter: str
    session_id: str
    state: TaskState
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_seconds: float = 0.0
    throttle_wait_seconds: float = 0.0
    configuration_version: str = ""
    configuration_hash: str = ""
    input_hash: str = ""
    output_hash: str = ""
    failure_class: str | None = None
    detail: str | None = None
    completed_at: str = field(default_factory=utc_now)

    @property
    def succeeded(self) -> bool:
        return self.state is TaskState.CANDIDATE_COMPLETE

    def as_body(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "alias": self.alias,
            "gateway": self.gateway,
            "adapter": self.adapter,
            "session_id": self.session_id,
            "state": str(self.state),
            "usage": self.usage,
            "latency_seconds": self.latency_seconds,
            "throttle_wait_seconds": self.throttle_wait_seconds,
            "configuration_version": self.configuration_version,
            "configuration_hash": self.configuration_hash,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "failure_class": self.failure_class,
            "emitted_tool_call": bool(self.tool_calls),
            "completed_at": self.completed_at,
        }


@runtime_checkable
class WorkerAdapter(Protocol):
    """Every worker adapter. The harness depends on this, never on a vendor."""

    #: Stable identifier used by the registry and recorded on every outcome.
    name: str

    #: ``True`` only when this adapter can actually run here and now.
    def is_available(self) -> bool: ...

    async def execute(self, work_unit: WorkUnit, decision: RoutingDecision) -> WorkerOutcome: ...
