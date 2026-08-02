"""Account-wide model-request throttle, shared across processes.

``model-policy.yaml -> request_policy`` records a measured fact: the upstream
enforces **100 requests per minute account-wide**, counting every request
regardless of which model it hits. Spreading load across models was tested at
N=10 in four conditions and does not help; only slowing down does. The policy
therefore sets a 90 rpm ceiling with a 0.9s minimum interval and declares
``unthrottled_fanout: forbidden``.

Why this is not "duplicating LiteLLM": DEC-002 forbids solving it in the proxy.
Queueing inside the gateway would hide 429s, and a 429 from the eval gateway is
*evidence*. The limiter therefore has to sit on our side of the wire. See
``docs/decisions/DEC-301``.

Why it is cross-process: six worktrees run concurrently on this host. A limiter
scoped to one interpreter would let six of them emit 6 x 90 rpm and self-inflict
429s that are indistinguishable from genuine model failure -- fabricated
evidence, which is worse than a slow build.

Mechanism: an advisory ``fcntl.flock`` over a small JSON file holding the
timestamps of recently *reserved* slots. A caller takes the lock only long
enough to reserve the next permissible instant, then releases it and waits. The
lock is never held across a sleep, so a slow worker cannot stall the fleet.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from models.policy import ModelPolicy, load_model_policy

#: Shared by every EFAH process on this host. Overridable for tests only.
DEFAULT_STATE_PATH = Path(
    os.environ.get("EFAH_THROTTLE_STATE", Path(tempfile.gettempdir()) / "efah-global-throttle.json")
)

_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ThrottleReservation:
    """A reserved dispatch instant. ``waited_seconds`` is evidence, not noise:
    a run that waited is a run that did not fabricate a 429."""

    scheduled_at: float
    reserved_at: float
    in_window: int

    @property
    def waited_seconds(self) -> float:
        return max(0.0, self.scheduled_at - self.reserved_at)


class GlobalThrottle:
    """Account-wide limiter. Scope is *account*, never per model or per role."""

    def __init__(
        self,
        *,
        max_requests_per_minute: int,
        min_interval_seconds: float,
        state_path: Path | str | None = None,
        scope: str = "account_wide_not_per_model",
    ) -> None:
        if max_requests_per_minute <= 0:
            raise ValueError("max_requests_per_minute must be positive")
        self.max_requests_per_minute = max_requests_per_minute
        self.min_interval_seconds = float(min_interval_seconds)
        self.scope = scope
        self.state_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_policy(
        cls, policy: ModelPolicy | None = None, *, state_path: Path | str | None = None
    ) -> GlobalThrottle:
        policy = policy or load_model_policy()
        rp = policy.request_policy
        if not rp.global_throttle_required:  # pragma: no cover - pack asserts True
            raise ValueError("pack disables the global throttle; unthrottled_fanout is forbidden")
        return cls(
            max_requests_per_minute=rp.max_requests_per_minute,
            min_interval_seconds=rp.min_interval_seconds,
            state_path=state_path,
            scope=rp.throttle_scope,
        )

    # -- reservation --------------------------------------------------------
    def reserve(self) -> ThrottleReservation:
        """Claim the next permissible dispatch instant. Does not sleep.

        Held under an exclusive file lock so that concurrent worktrees cannot
        both claim the same slot.
        """
        now = time.time()
        with self._locked_state() as (handle, slots):
            horizon = now - _WINDOW_SECONDS
            slots = [t for t in slots if t > horizon]

            scheduled = now
            if slots:
                scheduled = max(scheduled, slots[-1] + self.min_interval_seconds)
            if len(slots) >= self.max_requests_per_minute:
                # The oldest slot that must fall out of the window before this
                # request is allowed.
                oldest_blocking = slots[-self.max_requests_per_minute]
                scheduled = max(scheduled, oldest_blocking + _WINDOW_SECONDS)

            slots.append(scheduled)
            slots = slots[-(self.max_requests_per_minute * 2) :]
            self._write(handle, slots)
            in_window = sum(1 for t in slots if t > scheduled - _WINDOW_SECONDS)

        return ThrottleReservation(scheduled_at=scheduled, reserved_at=now, in_window=in_window)

    def acquire(self) -> ThrottleReservation:
        """Blocking acquire: reserve a slot, then wait for it."""
        reservation = self.reserve()
        delay = reservation.scheduled_at - time.time()
        if delay > 0:
            time.sleep(delay)
        return reservation

    async def acquire_async(self) -> ThrottleReservation:
        """Async acquire. The file lock is taken in a worker thread so a slow
        peer cannot block this process's event loop."""
        reservation = await asyncio.to_thread(self.reserve)
        delay = reservation.scheduled_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return reservation

    # -- introspection ------------------------------------------------------
    def requests_in_window(self) -> int:
        horizon = time.time() - _WINDOW_SECONDS
        with self._locked_state() as (_handle, slots):
            return sum(1 for t in slots if t > horizon)

    def reset(self) -> None:
        """Test-only: clear the shared window."""
        with self._locked_state() as (handle, _slots):
            self._write(handle, [])

    # -- internals ----------------------------------------------------------
    class _LockedState:
        def __init__(self, path: Path) -> None:
            self._path = path
            self._handle = None

        def __enter__(self):
            self._handle = open(self._path, "a+")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            self._handle.seek(0)
            text = self._handle.read()
            try:
                slots = [float(t) for t in json.loads(text)["slots"]]
            except (ValueError, KeyError, TypeError):
                slots = []
            slots.sort()
            return self._handle, slots

        def __exit__(self, *exc) -> None:
            assert self._handle is not None
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def _locked_state(self) -> GlobalThrottle._LockedState:
        return self._LockedState(self.state_path)

    @staticmethod
    def _write(handle, slots: list[float]) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"slots": slots}))
        handle.flush()
        os.fsync(handle.fileno())
