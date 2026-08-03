# FINDING-008 — the implementer's channel rate-limits; the probe reads it as down

**Raised:** 2026-08-02 · **Class:** measurement, no owner decision required
**Status:** OPEN — the remedy is a routing change, recorded here first
**Evidence:** `evidence/FINDING-008-implementer-channel-rate.json`.
`oracle_type: reproducible_empirical_benchmark`. No model judge in the path.

## What happened

The first real instruction through the new owner-instruction consumer came back
`REWORK_REQUIRED` / `AvailabilityProbeRequiredError` — correct, §11.1 requires an
availability record before first dispatch. Running the probe produced **14 of 15
roles UP** and one down:

```
DOWN implementer-i12  production  TRANSIENT_PROVIDER_FAILURE: 503
```

`implementer-i12` is the highest-volume role in the system and the one the
consumer uses.

## Hypotheses, and what killed each

The owner's standing instruction is that latency is not a constraint and a
proper debug run beats a quick win, so this was worked as §7.4 requires —
multiple hypotheses, each with a discriminating test — rather than by retrying
until it looked fine.

| | Hypothesis | Outcome |
|---|---|---|
| H-001 | transient upstream outage | supported, then refined — the failure is load-shaped |
| H-002 | the `codex-pro` channel is dead | **refuted** — 3 consecutive raw calls returned 200 |
| H-003 | the production gateway is degraded | **refuted** — 5 other production roles probed UP in the same run |
| H-004 | the model is not configured | **refuted** — that returns 400, not 503 |
| H-005 | the tool-call payload triggers it | **refuted** — the exact probe tool payload returned 200, with and without `tool_choice: auto` |
| H-006 | the channel rate-limits closely-spaced requests | **supported** |

H-005 is the one worth noting: it looked right. Every other role probed with
`tool_call=True`, the probe sends a tool, and the raw call I had compared
against did not. Replaying the *exact* probe payload returned 200 — and then the
plain call that had passed three times in a row started returning 503. That is
what identified the variable as **time**, not payload.

## The measurement

```
12 attempts, 15s apart  →  12/12 OK   (availability 1.0)
consecutive attempts    →  503
availability probe      →  fails every run
```

The probe issues 15 role probes back to back at the global throttle's 0.9s
floor. That floor is an **account-wide** limit (100 rpm measured, 90 enforced),
and it is correct at that scope. It is simply too fast for this one channel.

The upstream error names the shape:

```
litellm.ServiceUnavailableError: Service temporarily unavailable.
Received Model Group=gpt-5.6-luna
Available Model Group Fallbacks=None
```

A cooled-down deployment with no fallback group returns 503 for the whole group.

## Why it matters more than one red line on a probe

The router did the right thing and **substituted**: the consumer's first
successful instruction ran under `planchal-q06`, not `implementer-i12`. So the
system stayed up — by quietly running the highest-volume role on a different
model. That is a correct fallback and a bad steady state, and it would not have
been visible without reading the alias in the result record.

This also bears on the owner's frontier-first tiering directive. The instinct
when a model 503s is to route around it; the measurement says this model is
**100% available when asked at a sane rate**. Demoting it would trade a frontier
model for a weaker one to solve a spacing problem.

## Remedy (not yet applied)

- **Per-channel spacing**, not just the account-wide floor. The throttle is
  global by design (DEC-301) and needs a per-alias minimum interval on top for
  channels that cannot take 0.9s.
- **Probe pacing.** The availability probe should space its own requests wider
  than the account floor, or a busy channel reads as an outage — the probe
  currently manufactures the failure it reports.
- **Retry with backoff on the probe specifically.** A single 503 recorded as
  DOWN sends the highest-volume role to a substitute for the rest of the run.
  This is *not* a licence to retry gate-bearing calls: DEC-002 keeps
  `num_retries: 0` on the eval gateway, and the probe is availability
  measurement, not evidence.

Recorded before applying, because the fix touches the throttle — the one piece
of shared machinery that an unthrottled fan-out turns into fabricated evidence.
