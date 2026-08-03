# FINDING-010 — the sealed-holdout gate was measuring the exam, not the candidate

**Raised:** 2026-08-03 · **Class:** measurement · no owner decision required
**Status:** **FIXED same day** — minting and grading are now separate verbs.
**Evidence:** `evidence/generation-determinism-probe.json`,
`evidence/grade-reproducibility.json`, `evidence/sealed-exam-pin.json`,
`evidence/DEC-006-verifier-identity.json`, generator logs `skeleton-*` in the
verifier's sealed log.

## What happened

Twenty-five runs of `deploy/verifier/generator.py` against **one commit and one
contract version** produced twenty-five different reference implementations, and
the run passed roughly **45%** of the time. Two consecutive runs, three minutes
apart, on commit `40267175642c`:

```
15:26  claude-opus-4-8 → reference 2858 chars   kimi-k2.7-code → 3 mutants
       baseline exit 0 · mutant_1/2/3 BROKEN_RUN(exit 2) · kill_rate 0.0000 · REFUSED
15:29  claude-opus-4-8 → reference 2571 chars   kimi-k2.7-code → 3 mutants
       baseline exit 0 · mutant_1/2/3 KILLED     · kill_rate 1.0000 · ACCEPTED
```

Nothing about the candidate differed between those two runs. Everything about
the exam did.

## Where the non-determinism entered

Three places, and the third is why it compounded rather than averaged out:

1. **`call_model` sent no `seed` and no `top_p`.** The request body was
   `{model, messages, max_tokens, temperature: 0, stream, stream_options}`.
   Temperature 0 is not determinism; it is a preference expressed to a sampler
   that is free to break ties differently on each call, and per FINDING-005 and
   DEC-007 these roles are served from resold subscription pools where the
   serving stack can change between two requests a second apart.
2. **`generation_request_id` never reached a prompt.** The holdout prompt
   interpolated `candidate_commit` and `contract_version` only, so identical
   inputs produced byte-identical prompts — and non-identical output.
3. **The mutant prompt embedded the previous call's output.** It opened with
   *"Here is a correct Python module"* followed by `subject.read_text()`. So the
   subject was regenerated on every run **and** the mutants were regenerated
   against whatever subject had just been invented. Two independent samplers,
   chained.

## The cheap fix was measured first, and it does not work

`project-pack/evidence/owner-documents/CONFIGURATIONGUIDE.md` records `seed` as
*"universally supported across 21 models tested"*. That list is about
**parameter acceptance** — it sits beside `stop` and JSON mode, and it means the
gateway does not return 400. It is not a claim about reproducibility, and it was
worth an hour to find out which it was before designing anything.

`tools/probe_generation_determinism.py`, three identical requests per arm:

| model | arm | distinct outputs | `system_fingerprint` |
|---|---|---|---|
| `claude-opus-4-8` | `seed=20260803` | **3 / 3** | absent |
| `claude-opus-4-8` | no seed | 3 / 3 | absent |
| `kimi-k2.7-code` | `seed=20260803` | **3 / 3** | absent |
| `kimi-k2.7-code` | no seed | 3 / 3 | absent |

The seeded arm is indistinguishable from the control. There is also no
`system_fingerprint` on any response, so a client cannot tell an honoured seed
from one silently dropped — which means even a seed that appeared to work could
not be *shown* to have worked.

**So no `seed` is sent.** A parameter measured not to change the outcome, which
the client cannot verify was honoured, is a configuration line to point at
instead of a property to rely on. That is the manufactured confidence DEC-006
already refuses in a different form.

## The fix: mint once, freeze, grade many

`--mode MINT` authors the exam, runs the mutation gate, and freezes the set
under `store/exams/<hex>/` named for the hash of its own contents — only if
every seeded mutant died. `--mode GRADE --exam-id sha256:…` loads that exam,
re-verifies that it still hashes to its own name, and runs a candidate against
it. **No model participates in a grade.**

`--mode` has no default and `GRADE` without a pin is `EXAM_NOT_PINNED`, exit 8.
The old behaviour — grade against an exam minted in the same breath — is not the
default any more; it is unreachable.

## What it now measures

`evidence/grade-reproducibility.json`, one frozen exam
(`sha256:c0ae8a35…`, 1 holdout, 3 mutants, minted at kill_rate 1.0), six grade
runs per candidate:

| candidate | runs | distinct verdicts | verdict | per run |
|---|---|---|---|---|
| the exam's own frozen reference | 6 | **1** | exit 0, kill_rate 1.0, 3/3 | ~0.8s |
| a submitted implementation that does not satisfy the API | 6 | **1** | exit 7, `CANDIDATE_FAILED_HOLDOUTS` | ~0.4s |

"Distinct verdicts" compares **every** receipt field except `generated_at` and
`generation_request_id`, not merely the exit status — a run that reached the
same answer with a different kill count or against a different exam would pass a
looser comparison and would not be the property being claimed.

The two arms disagree with each other, which matters as much as the stability:
a gate that only ever says PASS is stable and useless.

The cost changed as well. A grade is **~0.8s and no model calls**; the run it
replaces was ~2m48s and two frontier completions, per skeleton run, at a 45%
pass rate. `tools/gate_dec_006.py` check E used to mint a real exercise on every
gate run for the sole purpose of confirming that a receipt has the right shape;
it now probes the unpinned-grade refusal instead, which costs nothing and asserts
something stronger.

## What this does not fix

- **Minting is still non-deterministic**, and nothing here makes it otherwise.
  It does not need to be. A mint that fails costs three minutes and produces
  nothing; a mint that succeeds produces an artifact with an identity that every
  later run can be bound to. Expect to run `tools/mint_sealed_exam.py` more than
  once.
- **A frozen exam is detected-not-prevented.** The files are owned by
  `efah-verifier`, which is the account that runs the generator, so the 0400/0500
  modes are a guard rail. `EXAM_CONTENT_HASH_MISMATCH` is the real protection:
  an exam that no longer hashes to its own name carries no verdict.
- **`GRADE` is an oracle the builder can query.** Repeated pass/fail answers leak
  the exam a bit at a time. That is inherent to any holdout gate rather than
  specific to this one, and DEC-006 already carries the shape of it as accepted
  debt. What is done about it: every grade names the exam it used, so a set that
  has been queried enough to be inferred is re-minted under a new identity.
- **`hidden_holdout` still does not report PASS.** Nothing in the release path
  has yet submitted a candidate. A reproducible gate is a precondition for that,
  not a substitute for it.
