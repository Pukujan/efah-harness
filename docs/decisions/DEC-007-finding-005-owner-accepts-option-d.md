# DEC-007 — FINDING-005 answered: option D, on the owner's own benchmark

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** `OWNER_RISK_ACCEPTANCE` (§10.7 permitted interrupt type)
**Answers:** blocker `FINDING-005-transport` (parts 1–3)
**Decided by:** Kujan (owner), 2026-08-02 · **Status:** DECIDED
**Owner's words:** *"d is fine ive tested and benchmarked them, they are reliable"*

## Decision

**Option D.** Keep the current ckff transport for all roles, including the nine
gate-bearing assurance roles. Record the provenance limitation honestly and
proceed. Sealed holdout generation is **unblocked**.

## Why this is stronger than D as it was originally framed

FINDING-005 offered D as *"accept and record… the evidence package must say the
assurance path's model provenance is unverified."* That framing assumed the
acceptance would be blind. It is not.

The owner supplied `Pukujan/private-study-log/projects/litellm-ckff-proxy`,
which is a measured benchmark of this exact transport, dated 2026-08-01:

- **`EVAL-LAB.md`** — *"All Tier A frontier models passed reliability testing
  (3/3) with streaming and tool-calling verified."*
- **`MODELS.md`** — a re-audit found *"45/45 reachable models emit tool calls,
  and zero do not,"* and the earlier apparent failures were **measurement
  artifacts from insufficient `max_tokens`**, not model defects.
- Latency distributions per model, including worst-case not just median
  (*"medians hide the problem"* — `gpt-5.6-sol` at 4.7s median, 119.4s worst).
- Per-channel *observed spend*, because *"the pricing table and the actual bill
  disagree, sometimes by orders of magnitude."*

So the accepted risk is **provenance, not capability**. Capability was measured
by the owner across 45 models before this build began. That is a materially
different acceptance from "we did not check", and the honest-debt entry is
rewritten to say so.

## What remains true, and stays in the ledger

The transport measurement is unchanged and is not contradicted by the benchmark
— the benchmark **corroborates** it. `EVAL-LAB.md` documents
*"claude-opus-5 on kiro-pro billed $0.0001 across 56 calls despite a $75/M list
rate."* The owner's own report names `kiro-pro`.

What stays recorded:

- Several role aliases resolve to shared upstream channels. Three
  anthropic-family assurance roles resolve to channel 234 / `kiro-pro`.
- A degraded assurance model does not error; it emits plausible output that
  passes. That failure mode is not eliminated by benchmarking, because a
  benchmark measures a model at a point in time and the pool can change.
- Therefore: **the mutation kill rate and any model-authored assurance artifact
  remain weaker evidence than their numbers suggest**, and the §27 package says
  so.

## Correcting the framing, not the measurement

The owner asked: *"wym kiro? I thought we have many providers."* Both are true
and the finding was stated too broadly.

**Many providers is correct.** The account carries **203 models across 23
channel prefixes and 12 vendors** — measured, `evidence/FINDING-005-transport-probe.json`.

**The narrow claim is also correct**, and is the one that matters: of the nine
*gate-bearing* roles, the three anthropic-family ones share channel 234.
Provider diversity at the account level does not give role separation at the
transport if the separated roles land on one channel.

FINDING-005 should have said "three assurance roles share a channel" rather than
leading with the supplier's name. The supplier is not the finding; the
concentration is.

## Parts 2 and 3 of the blocker

- **Part 2 (FINDING-006, family concentration):** subsumed by this acceptance.
  The alias map stays as the owner wrote it. The sixteen contract-required
  separation edges remain enforced in code and all sixteen still hold.
- **Part 3 (FINDING-003, tier labels):** the labels still contradict the models
  on a plain reading, and the owner's benchmark is the authority on capability
  rather than the label. No change to `model-policy.yaml`. Recorded, not acted on.

## Consequences, effective now

1. `hidden_holdout` is no longer blocked by an owner decision. The generator's
   refusal is lifted by writing the decision to
   `/var/lib/efah-verifier/etc/transport-decision` — inside the verifier's own
   `0700` directory, which the builder cannot write, so the owner's authority is
   what lifts it.
2. C's **private mutant corpus with known kill difficulty** is still worth
   building and is recommended regardless of A/C/D. It is the only check that
   measures assurance *capability* continuously rather than trusting a benchmark
   taken on one day.
3. `DEBT-003` in the §27 package is rewritten from "unverified and unmeasured"
   to "provenance unverified at the transport; capability independently
   benchmarked by the owner, 2026-08-01".
