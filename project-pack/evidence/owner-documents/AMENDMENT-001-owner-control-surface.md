# AMENDMENT-001 — Vendor-neutral owner control surface

**Amends:** EFAH-CONTRACT-001 v1.0 → **v1.1**
**Proposed:** 2026-08-02
**Class:** CONTRACT_AMENDMENT_REQUIRED → owner-approved pre-launch
**Process:** contract §1.3

---

## Why

`product.vendor_neutral_after_deadline: true` requires that no essential runtime
path depend on Claude access after 2026-08-03. The pack currently satisfies the
*execution* half of that: LangGraph, LiteLLM, TerminusDB, and CI all run without
Claude.

It does not satisfy the *control* half. On 2026-08-04 the owner's surfaces are:

| Surface | Post-deadline status |
|---|---|
| `claude remote-control` | **Gone** — dies with Claude Code access |
| `harness project run` CLI | Requires a terminal; not usable from a phone |
| Plane | Read projection only; `mode: projection_only`, cannot drive the harness |
| FastAPI endpoints (§11.3) | Exist, but no interface a human can use from a phone |

So a harness that runs autonomously but that the owner cannot steer, resume,
answer, or redirect from the device they actually have is vendor-neutral in
execution and Claude-dependent in practice. That is a gap in the contract's own
stated property, not a feature request.

## The clause

Add to §11 (Model Router, Middleware, Controllers, and Views) as **§11.7**:

> **§11.7 Owner control surface.** The system MUST expose a vendor-neutral
> control surface through which the owner can, without Claude Code and from a
> mobile device: observe current project and task state; answer an open typed
> owner blocker; resume, retry, or cancel a work unit; and issue a new
> contract-bounded instruction that enters the normal gate path.
>
> The surface MUST be implemented as a LangGraph-backed conversational endpoint
> on the existing FastAPI application, reachable over the operator's private
> network. It MUST NOT be a second orchestrator: it holds no authority the API
> and contract do not already grant, it cannot change scope, and every command
> it accepts is a request that enters the same validation, drift, and gate path
> as any other input.
>
> The surface MUST be exercised in the walking skeleton and MUST be proven to
> function with all Anthropic credentials removed from the environment.

## Impact analysis

**Requirements affected**

- `product.vendor_neutral_after_deadline` — this closes the control-path gap.
- §11.3 API router — adds endpoints; no change to the router's rule that it maps
  endpoints to controllers only.
- §11.6 dashboard views — the surface consumes the same read projections; it
  does not introduce a parallel state source.
- §14.4 walking skeleton — the trace gains one step: `owner control surface`
  after `dashboard update`.
- §10.7 human interrupts — unchanged. The surface is how the owner *answers* a
  typed blocker; it does not create new interrupt types.

**Explicitly NOT granted.** The surface is not a free-form project-manager agent
with authority to change scope (contract §0 and `non_goals`). It cannot approve
its own requests, bypass a gate, alter the contract, or reach protected assets.
An instruction that would expand scope produces `UNAPPROVED_SCOPE_EXPANSION`,
exactly as the same instruction typed at the CLI would.

**Model routing.** The surface routes through the **production** LiteLLM gateway
per DEC-002 — it produces candidate work, not gate-bearing evidence.

**Cost.** Real, and paid deliberately: this competes for build time in a
compressed window. It is accepted because a harness the owner cannot drive after
2026-08-03 is a harness that stops on 2026-08-03, which forfeits more than the
features it displaces.

## New acceptance check

Add to `acceptance_checks`:

```
owner_control_surface_vendor_neutral
```

Gated by `GATE-D1-10-owner-control-surface.yaml`. Day 1, blocking — Day 1
because if it slips to Day 3 in a compressed window it does not get built, and
it is the single capability that determines whether work continues after the
builder leaves.

## Priority

This amendment does not lower the priority of the walking skeleton. It adds one
step to it. If time forces a choice, the order is:

1. complete walking skeleton, end to end, no placeholders
2. vendor neutrality proven with Anthropic credentials removed (`GATE-D1-07`)
3. **owner control surface** (`GATE-D1-10`)
4. everything else

Items 1–3 are what the owner still has on 2026-08-04. Item 4 is what the owner
can build themselves, using 1–3.

## Owner approval

```
Approved by:   Kujan (github: Pukujan)
Date:          2026-08-02
Supersedes:    EFAH-CONTRACT-001 v1.0
New version:   EFAH-CONTRACT-001 v1.1
Status:        APPROVED
```

Remaining §1.3 steps are the builder's, at intake: attributable TerminusDB
commit, recompiled workflow and gate definitions, and revalidation of affected
tasks, artifacts, tests, oracles, and release candidates.
