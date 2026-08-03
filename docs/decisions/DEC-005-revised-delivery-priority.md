# DEC-005 — Revised delivery priority (owner)

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** `OWNER_PRIORITY_DECISION` (contract §10.7 permitted interrupt type)
**Date:** 2026-08-02 · **Decided by:** Kujan (owner) · **Status:** BINDING

## Decision

Supersedes `project.yaml → delivery_priority`:

1. complete walking skeleton, end to end, zero placeholders
2. **owner control surface (GATE-D1-10)** — served from `gravebuster`, reachable
   over the tailnet from a mobile viewport
3. vendor neutrality proven with Anthropic credentials removed (GATE-D1-07)
4. everything else

Items 2 and 3 exchange places relative to `project.yaml`.

## Owner's stated success condition

> By end of 2026-08-02 the owner must be able to open a phone, reach
> `gravebuster` over the tailnet, and drive a working harness.

## Rationale (owner)

GATE-D1-07 is a property test over a finished system and cannot be meaningfully
run before items 1 and 2 exist. Building it earlier proves nothing. Items 1 and 2
are the deliverable; item 3 verifies it.

## What this does NOT change

**GATE-D1-07 remains blocking and must pass before any merge to `main`.** It moves
in sequence only. This is a reordering, not a weakening, and therefore is a
priority decision under §10.7 rather than a §1.3 contract amendment — no
acceptance check, gate assertion, or auto-merge requirement changes.

`project.yaml → silent_reordering: forbidden` is satisfied: the reorder is
explicit, owner-authored, recorded here, and bound to contract v1.1.

## Concurrency directive (owner)

Host `gravebuster`: 16 cores, 24 GiB free. Independent Day-1 workstreams run
concurrently in separate git worktrees, **not** serialized. Target 4–6 concurrent
work units; back off if load average exceeds 12.

Declared independent after the kernel lands: control-plane schemas · LangGraph
graph skeletons · model-router path · Plane projection adapter · CI.

The global **90 req/min account-wide** model throttle
(`model-policy.yaml → request_policy`) holds **regardless of worktree count**. An
unthrottled fan-out self-inflicts 429s that are indistinguishable from genuine
model failure, which would be fabricated evidence.

## Recompilation

The compiler re-emits phase ordering and the Day-1 gate schedule from this
record. GATE-D1-10 moves ahead of GATE-D1-07 in the execution schedule; both
remain `blocking: true` and both remain merge-blocking for `main`.
