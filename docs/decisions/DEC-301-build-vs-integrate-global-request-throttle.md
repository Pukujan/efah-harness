# DEC-301 — BUILD_VS_INTEGRATE: the account-wide request throttle is harness-side

**Bound to:** EFAH-CONTRACT-001 v1.1 · §14.2 (dependency-first gate), §4.1
**Class:** `BUILD_VS_INTEGRATE`
**Date:** 2026-08-02 · **Workstream:** WS-D (model router / gateways / workers)
**Status:** RECORDED

## The question

`dependency-policy.yaml` lists `custom_provider_router` as prohibited —
LiteLLM owns provider routing, and rebuilding any part of it fails the scope
gate. A rate limiter looks like part of a provider router. Is
`src/models/throttle.py` a reimplementation?

## Decision

Build it, harness-side. It is not a provider router and it cannot live in the
proxy.

## Why the integrated option is unavailable

1. **DEC-002 forbids it.** "Because there is no `max_parallel_requests` on the
   eval service, the harness must throttle globally … Queueing inside the proxy
   would hide 429s that are themselves evidence." A 429 from the eval gateway is
   a recordable fact under §18; a proxy that absorbs it destroys the evidence
   the gateway exists to produce.
2. **The eval deployment is DB-less by design.** Per-key budgets and rate limits
   in LiteLLM require the database. DEC-002 records the absence of that database
   as the isolation mechanism and forbids attaching a `DATABASE_URL`. So the
   proxy-side feature is not merely undesirable here — it is unreachable.
3. **The limit is account-wide across processes.** The measured constraint is
   100 req/min counted upstream across every model and every caller. Six
   worktrees run concurrently on this host. Neither LiteLLM deployment can see
   the other's traffic, and neither can see a sibling worktree's.

## What was built, and its bounds

`GlobalThrottle` — ~120 lines. An `fcntl.flock` over a JSON file of reserved
dispatch instants in the system temp directory, shared by every EFAH process on
the host. Callers reserve a slot under the lock and wait outside it, so a slow
worker never stalls the fleet. Values come from
`model-policy.yaml → request_policy` (90 rpm, 0.9s minimum interval, scope
`account_wide_not_per_model`); none are hardcoded.

It does **not** select providers, retry, fail over, pool, or transform requests.
Every one of those remains LiteLLM's.

## Known limits

The file lock is host-local. If the harness is ever distributed across hosts the
state file must move to a shared store; the interface does not change.

## Evidence

`tests/unit/test_worker_throttle.py::test_the_window_is_shared_across_processes`
spawns two interpreters and asserts they produce a single 0.9s-spaced ladder
rather than two independent ones.
