# DEC-008 — a model failure is a configuration finding, not a verdict

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** owner methodology directive · **Status:** BINDING
**Decided by:** Kujan (owner), 2026-08-02

## The rule, in the owner's words

> "do not assume ckff model fail based on few failures always try to debug them
> through multiple ways multiple hypothesis and make sure they work…
> they mostly always work u just have to keep trying more fuzzy configuration
> till they work right… in fact might be best for you to actually run them
> through series of test for data on how to use them…
> **try taking positive result more than negative as most are false negative and
> jumped conclusion just like you did**"

## Why it was needed

The builder earned this rule. In one session it:

1. Reported a **fabricated `kill_rate: 1.0`** because the verifier identity had
   no pytest and `python -m pytest` exits 1 exactly like a failing test;
2. Concluded `gpt-5.6-luna` was rate-limited from a burst probe, when the
   owner's own study log had already documented the cause and the remedy;
3. Wrote **FINDING-009** declaring `claude-opus-5-thinking` unable to do
   long-form generation, after three attempts varying two parameters;
4. Declared `kimi-k2.7-code` unusable after **one** empty response.

Items 3 and 4 were both wrong, and a configuration sweep found working cells for
both models **on the first grid position tried**.

The pattern is not carelessness about any one model. It is that a negative
result feels like information and a positive result feels like luck, when on
this transport the reverse is closer to true.

## The asymmetry, stated so it can be applied

**One success proves the model can do the task. One failure proves nothing.**

A working configuration is a fact about the model. A failing configuration is a
fact about *that configuration*, and the search space includes at least:
`max_tokens`, `stream`, task size, prompt shape, concurrency, pacing, and
repetition. A verdict may only be recorded after the space has been searched and
the search itself recorded.

## What this changed immediately

`tools/fuzz_generation_config.py` sweeps that grid and **stops at the first
working cell** rather than averaging failures into a score. It found:

```
OK  kimi-k2.7-code          tok=6000  stream=False  n=3
OK  claude-opus-5-thinking  tok=6000  stream=False  n=3
```

And it pointed at the real cause, which the owner's cortex research had already
recorded on 2026-07-19:

> "HTTP 524 on long gens (~120s/~16k) was a **NON-STREAM artifact** …
> streaming keeps the connection alive → long gens COMPLETE
> (verified 139.5s/~3.2k tok)."

The generator was calling `urllib` **non-streaming**. It reproduced the same
artifact with different status codes — HTTP 408 at 8000 and 4000 `max_tokens`,
HTTP 502 at 16000 — all after the client timeout had been raised to 300s and
proven not to be the binding constraint. A silent connection held open while a
model works gets closed by something in the path; a streamed one does not.

Switching the generator to streaming produced, on the first run:

```
claude-opus-4-8   5533 chars   streamed=True
kimi-k2.7-code   14587 chars   streamed=True   <- the model "proven" unusable
baseline (correct subject): pytest exit 0
mutant_1..5: KILLED    kill_rate 1.0   mint_accepted true
```

`kimi-k2.7-code` had been written off on the strength of a single empty
response. It emits 14.5k characters when the connection is kept alive.

## Standing obligations

- **Sweep before concluding.** No model may be recorded as failing a task until
  a configuration sweep has run and is attached as evidence.
- **Prefer the positive.** Where results disagree across configurations, the
  successful one is the finding. Record the failing configurations as
  constraints on *use*, not as properties of the model.
- **Streaming is the default for any long generation** on this transport, and
  the reason is recorded above rather than left as folklore.
- **A prohibition needs a sweep, not a sample.** `model-policy.yaml →
  prohibited_models` entries predating this rule are suspect; two of them
  (`gpt-5.6-sol`, `kimi-k2.7-code`) have already been lifted on measurement.

## What this does not license

Retrying until something passes and reporting the pass is the opposite failure
and is still forbidden. DEC-002 stands: gate-bearing evidence runs with zero
retries, and a retry the recorded configuration does not mention makes the
evidence unprovable. The sweep is for *establishing how to use a model*; once
established, the configuration is recorded and the gate-bearing run happens once.

FINDING-009 has been corrected rather than deleted — it was a real measurement
with a wrong conclusion, and the difference is the point.
