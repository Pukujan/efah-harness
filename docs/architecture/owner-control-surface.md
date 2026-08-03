# Owner control surface — architecture note

Contract **EFAH-CONTRACT-001 v1.1 §11.7**, added by AMENDMENT-001 and promoted to
delivery priority 2 by DEC-005.

## Why it exists

`product.vendor_neutral_after_deadline: true` was satisfied only in *execution*.
On 2026-08-04 the owner's control surfaces would have been: `claude
remote-control` (gone), the CLI (needs a terminal), Plane (read-only projection),
and raw FastAPI endpoints (no human interface). A harness that runs autonomously
but cannot be steered from the device the owner actually has is vendor-neutral in
execution and Claude-dependent in practice.

## Shape

```
phone (tailnet) → FastAPI router → LangGraph graph → gateway → TerminusDB
                                        │
                                   parse → classify → apply → record
                                             │
                                        (deterministic policy — no model)
```

`classify` runs **before** anything is applied and contains no model call. A
model-mediated authority check would let a persuasive instruction argue past the
contract, which is the `free_form_llm_orchestrator` non-goal in another costume.

The natural-language step is confined to `parse` and is optional — an explicit
verb from the UI bypasses it entirely. That is what lets the surface run with
every Anthropic credential removed.

## Authority limits (enforced, not documented)

| Limit | Mechanism | Gate |
|---|---|---|
| Cannot expand scope | `policy.SCOPE_EXPANSION_PATTERNS` → `UNAPPROVED_SCOPE_EXPANSION` | A6 |
| Cannot bypass a gate or self-approve | `policy.GATE_BYPASS_PATTERNS` | A7 |
| Cannot reach protected assets | `policy.PROTECTED_TERMS`, checked for **reads too** | A8 |
| Cannot apply a stale contract | version binding on every command | drift |
| Cannot mark anything PASSED | only gates write `PASSED` (`governance.states`) | §9.3 |

Every command — including every refusal — is recorded with a content hash and an
attributable record id. A refusal that left no trace would be unauditable.

## Serving

```bash
uvicorn owner_surface.app:app --host 100.93.66.35 --port 8088
```

Bound to the tailnet address: reachable from the owner's phone on the private
network, and from nowhere else. The page is entirely self-contained — no CDN, no
external font, no framework — because a phone on a tailnet may have no public
internet path at the moment the owner needs to steer the build.

## Deliberate limits

- The surface reads through `ControlPlaneGateway`. Until the authoritative graph
  is populated it reports `graph not yet initialised` rather than inventing
  numbers. A control surface showing confident state it did not read is worse
  than one that admits it cannot see.
- It is mounted as a router, so it also composes onto the main application; the
  standalone app exists so the owner can still reach it while the rest of the
  control plane is mid-repair.
