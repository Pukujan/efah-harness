"""Fresh, bounded worker sessions (contract Section 10.5, GATE-D1-05).

Independent worker roles MUST use fresh per-invocation sessions. Persistent
conversational memory is prohibited by default; long-running project memory
belongs in TerminusDB, artifacts, and checkpoints -- not in model chat context.

The rules are enforced by construction rather than by convention:

* a session is created *by* an invocation and starts with ``prior_turn_count``
  zero (GATE-D1-05 A1);
* the prompt is assembled from the work unit's declared inputs only, so context
  is bounded by the work unit and not by the project (A3);
* a closed session cannot be reused -- reuse raises rather than quietly carrying
  a transcript forward;
* the transcript is discarded when the session closes. What survives is the
  work unit's output plus its hashes, which the caller persists durably (A4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from governance.envelope import content_hash, utc_now
from models.errors import SessionReuseError
from models.policy import SessionPolicy


@dataclass(frozen=True)
class WorkUnit:
    """The bounded unit of work a session may see. Nothing else enters context."""

    task_id: str
    role: str
    instructions: str
    inputs: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 512
    tools: tuple[dict[str, Any], ...] = ()
    system: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "instructions": self.instructions,
            "inputs": self.inputs,
            "max_tokens": self.max_tokens,
            "tool_names": [t.get("function", {}).get("name") for t in self.tools],
        }

    @property
    def input_hash(self) -> str:
        return content_hash(self.as_body())


class WorkerSession:
    """One invocation, one session. There is no ``resume``."""

    def __init__(self, work_unit: WorkUnit, *, alias: str, session_policy: SessionPolicy | None = None) -> None:
        if session_policy is not None and not session_policy.fresh_per_invocation_worker_sessions:
            raise SessionReuseError(
                "session_policy.fresh_per_invocation_worker_sessions is false; the contract "
                "Section 10.5 default may not be disabled here"
            )
        self.session_id = f"sess-{uuid.uuid4().hex[:16]}"
        self.work_unit = work_unit
        self.alias = alias
        self.opened_at = utc_now()
        self.closed = False
        #: Contract Section 10.5 / GATE-D1-05 A1. Always zero at invocation.
        self.prior_turn_count = 0
        self._turns: list[dict[str, Any]] = []

    @classmethod
    def open(cls, work_unit: WorkUnit, *, alias: str, session_policy: SessionPolicy | None = None) -> WorkerSession:
        return cls(work_unit, alias=alias, session_policy=session_policy)

    def messages(self) -> list[dict[str, Any]]:
        """Build the prompt from the work unit alone.

        The payload scope is ``work_unit_inputs_only``. Nothing from another task,
        another session, or the project at large is admitted here.
        """
        self._assert_open()
        messages: list[dict[str, Any]] = []
        if self.work_unit.system:
            messages.append({"role": "system", "content": self.work_unit.system})
        body = [self.work_unit.instructions]
        if self.work_unit.inputs:
            body.append("Work-unit inputs:")
            for key in sorted(self.work_unit.inputs):
                body.append(f"- {key}: {self.work_unit.inputs[key]}")
        messages.append({"role": "user", "content": "\n".join(body)})
        return messages

    def record_turn(self, role: str, content: str) -> None:
        self._assert_open()
        self._turns.append({"role": role, "content": content})

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def close(self) -> dict[str, Any]:
        """Close and discard the transcript, returning only durable facts."""
        self._assert_open()
        summary = {
            "session_id": self.session_id,
            "task_id": self.work_unit.task_id,
            "role": self.work_unit.role,
            "alias": self.alias,
            "opened_at": self.opened_at,
            "closed_at": utc_now(),
            "turns": len(self._turns),
            "input_hash": self.work_unit.input_hash,
        }
        self._turns.clear()
        self.closed = True
        return summary

    def _assert_open(self) -> None:
        if self.closed:
            raise SessionReuseError(
                f"session {self.session_id} is closed; contract Section 10.5 requires a fresh "
                "session per invocation and prohibits carrying conversational memory forward"
            )

    def __repr__(self) -> str:
        return (
            f"WorkerSession(session_id={self.session_id!r}, alias={self.alias!r}, "
            f"prior_turn_count={self.prior_turn_count}, closed={self.closed})"
        )
