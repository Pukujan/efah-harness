# DEC-001 — LangGraph is the permanent runtime; Eval-lab's Temporal skeleton is pattern source only

**Decision ID:** DEC-001
**Bound to:** EFAH-CONTRACT-001 v1.0
**Class:** OWNER_SCOPE_DECISION (recorded pre-launch, not asked at runtime)
**Status:** DECIDED — signed 2026-08-02
**Date:** 2026-08-01

---

## Why this record exists

The existing `Eval-lab` repository contains a Temporal-based skeleton. The
contract lists `temporal_initial_runtime` under `non_goals` and names LangGraph
as `permanent_runtime`. A builder inspecting the repo will find working Temporal
code and a contract forbidding it, and will correctly classify that as a
conflict.

Without this record, that conflict resolves in one of two bad ways:

1. It consumes a slot in the single allowed owner question round
   (`max_initial_owner_question_rounds: 1`) — a round that should be spent on
   decisions only the owner can make, not on a conflict already settled.
2. The builder reinterprets it on its own, which contract Section 0 forbids
   ("do not reinterpret or broaden the selected architecture") and which
   Section 19.2 would later flag as `UNAPPROVED_SCOPE_EXPANSION`.

Recording it here resolves the conflict through the highest authority
(Section 1.2, priority 1) before the builder starts.

## Decision

**LangGraph is the permanent workflow runtime.** The initial checkpointer is
`AsyncSqliteSaver`, behind a checkpoint adapter, non-authoritative and
replaceable (contract Section 10.3).

**Eval-lab's Temporal skeleton is reusable evidence and pattern source, not the
base.** The builder may read it, port its gate definitions, dossier format,
ADRs, and threat model, and cite it as prior art. The builder MUST NOT carry
Temporal into the runtime, add it as a dependency, or propose it as an
alternative checkpoint backend.

## Authority

| Authority level | Source | Says |
|---|---|---|
| 1 | This contract, v1.0 | `permanent_runtime: langgraph`; `non_goals: temporal_initial_runtime`; Section 4.1 "Temporal is not part of the initial critical path" |
| 4 | Eval-lab repository state | Working Temporal skeleton exists |

Contract Section 1.2: no artifact may override a higher authority. Level 1 wins
over level 4. The contract supersedes the repository.

## Consequences accepted

- Existing Temporal orchestration work in Eval-lab is not carried forward as
  runtime. Its *value* is preserved as ported patterns, not as running code.
- LangGraph's `AsyncSqliteSaver` is single-host. If the deployment later needs
  multiple independent workflow worker processes writing concurrently, the
  adapter is swapped for another officially supported durable checkpointer
  (Section 10.3) — **not** for Temporal, which remains a non-goal.
- Because the checkpoint store is explicitly non-authoritative, this swap does
  not touch domain schemas or project authority. That is the point of the
  adapter.

## What would change this

Only a contract amendment under Section 1.3, requiring all seven of: an exact
proposed clause, impact analysis, owner approval, a new contract version, an
attributable TerminusDB commit, recompiled workflow and gate definitions, and
revalidation of affected artifacts.

A builder encountering friction with LangGraph is **not** grounds for revisiting
this. That is the `convenience_gradient_correction` methodology (M-18): the
easier path is not thereby the authorized one.

## Owner signature

```
Decided by:    Kujan (github: Pukujan)
Date:          2026-08-02
Contract bind: EFAH-CONTRACT-001 v1.0
Status:        SIGNED
```


