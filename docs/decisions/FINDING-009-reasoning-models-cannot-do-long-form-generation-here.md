# FINDING-009 — a reasoning model that passes every probe cannot do the job

> **CORRECTION, same day.** The title is wrong and so was the conclusion. The
> models were fine; the *transport* needed streaming. Non-streaming long
> generations were closed in flight — HTTP 408 and 502 — which the owner's cortex
> research had already identified as a NON-STREAM artifact on 2026-07-19.
> With streaming enabled, `kimi-k2.7-code` emitted 14,587 characters and the
> mint completed with all five mutants killed.
>
> The measurements below are accurate. The inference from them was a jumped
> conclusion, and DEC-008 exists because of it. Kept rather than deleted: the
> difference between a wrong conclusion and a deleted one is the whole record.

**Raised:** 2026-08-02 · **Class:** measurement · no owner decision required
**Status:** **CORRECTED 2026-08-02 — the conclusion was wrong.** See DEC-008.
**Evidence:** `evidence/model-requalification-opus5t.json`, generator logs
GEN7–GEN9 in the verifier's sealed log.

## What happened

The owner asked for `claude-opus-5-thinking` on `sealed_holdout_author` — the
strongest available model on the role whose output nothing downstream checks.
It was a good call on every number available at the time:

```
claude-opus-5-thinking   non_streaming  4/4 ok  tool=1.0  p50=2.48s  max=2.55s
claude-opus-5-thinking   streaming      4/4 ok  tool=1.0  p50=2.39s  max=2.56s
```

8/8, perfect tool calling, the fastest tail of anything measured. It also
resolves to **channel 263**, not the `kiro-pro` channel 234 that FINDING-005
found three assurance roles sharing — so it improved transport diversity too.

**And it cannot generate a holdout set.** Three consecutive attempts:

| run | max_tokens | client timeout | result |
|---|---|---|---|
| GEN7 | 8000 | 120s | socket read timeout → `TRANSIENT_PROVIDER_FAILURE` |
| GEN8 | 8000 | **300s** | **HTTP 408** from the upstream |
| GEN9 | 4000 | 300s | **HTTP 408** from the upstream |

Raising our own timeout from 120s to 300s changed the error from *ours* to
*theirs*, which is the useful part: **our client was never the binding
constraint.** The gateway returns 408 before a reasoning model finishes a
generation-sized request, and that timeout is not ours to raise.

## Why the probe could not have caught this

The availability probe and the re-qualification probe both ask for a **short**
answer — 512 and 1024 tokens. A reasoning model spends budget on reasoning
before it emits anything, so the cost of that hidden phase scales with the task,
not with the probe. A model can therefore be:

* **available** — 15/15 on the fleet probe,
* **capable** — 100% tool calls in both modes,
* **fast** — p50 2.4s,

and still fail the only task it was assigned. Every measurement was correct and
none of them measured the thing that mattered.

This is the same shape as FINDING-008, where a probe at the account rate floor
manufactured the outage it reported, and as the `max_tokens` artifact the
owner's own `MODELS.md` had already identified once. The recurring lesson is
narrower than "probes lie": **a probe measures the request it makes**, and a
probe whose shape differs from production work is evidence about the probe.

## Resolution

`sealed_holdout_author` reverted to `claude-opus-4-8`, which completes the full
generation — GEN6 minted a set with the baseline passing and six mutants each
killed individually.

The 300s client timeout is **kept**, and recorded in `model-policy.yaml` beside
the role. It is not a workaround for anything now, and it does not weaken
DEC-002: that decision is about *retries* making the recorded run differ from
the run that happened, and this is one attempt, still zero retries, allowed to
finish. Removing it would only mean the next long generation fails as ours
rather than theirs, which is strictly less informative.

## What this costs, stated plainly

The strongest model available, on the role that most needs it, is unusable for
that role on this transport. `sealed_holdout_author` stays on `claude-opus-4-8`,
which keeps three anthropic-family roles in assurance and keeps two of them on
channel 234 — the concentration FINDING-006 raised and DEC-007 accepted.

## What would fix it

- **A generation-capable route.** If any official-channel model can hold a long
  request open, the role could move there and gain verifiable provenance at the
  same time. `[官3] kimi-k3` and `[官4][量] glm-5.2` are candidates and are still
  **not configured on the eval gateway** — they return 400.
- **Chunked generation.** Author the subject, the tests and the mutants in
  separate smaller calls rather than one large one. More requests against an
  account-wide rate limit, and more places for a partial result to hide, so it
  is not obviously the better trade — recorded as an option, not a plan.
