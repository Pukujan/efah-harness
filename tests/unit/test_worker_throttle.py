"""T-034 — the global throttle is account-wide and crosses process boundaries.

``model-policy.yaml -> request_policy``: 90 rpm, 0.9s minimum interval, scope
``account_wide_not_per_model``, ``unthrottled_fanout: forbidden``. Six worktrees
run concurrently on this host, so a limiter that only holds inside one
interpreter would let the fleet self-inflict 429s -- and a self-inflicted 429
recorded as a model failure is fabricated evidence.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from models.policy import load_model_policy
from models.throttle import GlobalThrottle

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def throttle(tmp_path):
    return GlobalThrottle(
        max_requests_per_minute=90,
        min_interval_seconds=0.9,
        state_path=tmp_path / "throttle.json",
    )


def test_policy_values_are_read_from_the_pack(tmp_path):
    policy = load_model_policy()
    limiter = GlobalThrottle.from_policy(policy, state_path=tmp_path / "t.json")
    assert limiter.max_requests_per_minute == 90
    assert limiter.min_interval_seconds == pytest.approx(0.9)
    assert limiter.scope == "account_wide_not_per_model"
    assert policy.request_policy.unthrottled_fanout == "forbidden"


def test_consecutive_reservations_are_spaced_by_the_minimum_interval(throttle):
    first = throttle.reserve()
    second = throttle.reserve()
    third = throttle.reserve()
    assert second.scheduled_at - first.scheduled_at >= 0.9
    assert third.scheduled_at - second.scheduled_at >= 0.9


def test_the_minute_window_is_enforced(tmp_path):
    """The 91st request inside a minute is pushed past the window edge."""
    limiter = GlobalThrottle(
        max_requests_per_minute=5, min_interval_seconds=0.0, state_path=tmp_path / "t.json"
    )
    scheduled = [limiter.reserve().scheduled_at for _ in range(6)]
    assert scheduled[5] - scheduled[0] >= 60.0
    assert scheduled[4] - scheduled[0] < 60.0


def test_acquire_actually_waits(throttle):
    throttle.acquire()
    started = time.perf_counter()
    throttle.acquire()
    assert time.perf_counter() - started >= 0.85


async def test_async_acquire_waits_without_a_second_event_loop(throttle):
    """Two acquisitions must be at least one interval apart.

    Measured from *before* the first acquisition, not between the two. The
    throttle schedules relative to the reservation it granted, so if this
    process is descheduled between the two calls -- routine on a box running six
    worker lanes -- the remaining sleep is legitimately shorter than the
    interval while the spacing is still correct. Timing the gap between the
    calls measured CPU starvation, not the limiter, and failed under exactly the
    concurrency this limiter exists to survive.
    """
    started = time.perf_counter()
    await throttle.acquire_async()
    await throttle.acquire_async()
    assert time.perf_counter() - started >= 0.85


def test_the_window_is_shared_across_processes(tmp_path):
    """The trap this exists for: two worktrees, one account-wide budget."""
    state = tmp_path / "shared.json"
    script = (
        "import json,sys;"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r});"
        "from models.throttle import GlobalThrottle;"
        "t=GlobalThrottle(max_requests_per_minute=90, min_interval_seconds=0.9, "
        f"state_path={str(state)!r});"
        "print(json.dumps([t.reserve().scheduled_at for _ in range(3)]))"
    )

    def run() -> list[float]:
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        return json.loads(out.stdout)

    first = run()
    second = run()
    combined = sorted(first + second)
    gaps = [b - a for a, b in itertools.pairwise(combined)]
    assert all(gap >= 0.89 for gap in gaps), gaps
    # A per-process limiter would have produced two independent ladders that
    # interleave with ~0s gaps; a shared one produces a single ladder.
    assert min(second) > max(first) - 1e-6


def test_reset_clears_the_shared_window(throttle):
    throttle.reserve()
    assert throttle.requests_in_window() >= 1
    throttle.reset()
    assert throttle.requests_in_window() == 0
