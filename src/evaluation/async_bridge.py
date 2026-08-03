"""Drive a coroutine to completion from synchronous check code.

Check bodies are synchronous by contract -- a ``Check`` is
``(GateContext, GateSpec, AssertionSpec) -> AssertionOutcome`` -- but several
checks must exercise async adapters: the LangGraph checkpointer, the protected
identity store, the blinded alias store. Those checks reached for
``asyncio.run``.

``asyncio.run`` raises ``RuntimeError: asyncio.run() cannot be called from a
running event loop``. The walking skeleton drives the gate lane from inside one
-- ``composition/root.py:382`` calls ``GateRunner().run()`` and that call is
awaited by ``run_walking_skeleton``. So every check using ``asyncio.run``
raised there, was swallowed by a surrounding ``except Exception``, and left its
coroutine un-awaited (the ``RuntimeWarning`` seen in the 2026-08-03 skeleton
run).

The consequence is the part that matters: **the same gate PASSED standalone and
FAILED inside the skeleton, from the same code on the same tree.** A verdict
that depends on the caller's async context is not deterministic, which is
precisely what Section 17.2 forbids of a gate.

This bridge makes both paths identical. No running loop: ``asyncio.run``. A
running loop already on this thread: a worker thread with a loop of its own.
Either way the coroutine is awaited to completion, so none is ever abandoned.

Stdlib only, by design -- ``oracles/no_judge.py`` walks the import closure of
the evaluation modules and fails on any network or model root.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

__all__ = ["run_sync"]


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion and return its result, loop or no loop.

    Exceptions raised inside the coroutine propagate unchanged, so a check that
    wraps this call in ``try/except`` still sees the failure it expects rather
    than a transport artefact of how it happened to be invoked.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop on this thread: the ordinary path.
        return asyncio.run(coro)
    # A loop is already running here. Hand the coroutine to a thread that has
    # no loop of its own; ``asyncio.run`` there is legal and blocking on the
    # future keeps the caller's semantics identical to the no-loop path.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="efah-gate-async") as pool:
        return pool.submit(asyncio.run, coro).result()
