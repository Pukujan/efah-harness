# PROPOSED-AMENDMENT-001 — typed `expected_observations` and `discriminating_tests`

**Status: PROPOSED. Not applied. Requires an owner amendment to `project-pack/`.**

`project-pack/**` is builder-read-only — `src/contracts/compiler.py` sets
`read_only_paths: ["project-pack/**"]`, and `contract.md#14.3` hashes the visible
assertions. Neither `contract.md` §7.4 nor `methodology-policy.yaml` was touched
in the change that produced this document. What follows is the schema change the
owner would have to make, and exactly what it buys.

| | |
|---|---|
| **Gate** | `GATE-D2-14` — competing hypotheses and discriminating test |
| **Blocks** | A3 `discriminating_test_presence`, A4 `selection_provenance_check` |
| **Touches** | `project-pack/contract.md` §7.4, `project-pack/methodology-policy.yaml` `hypothesis_discipline` |
| **Unstages** | `src/evaluation/checks_d2_14.py::d2_14_a3`, `::d2_14_a4` |
| **Does not touch** | A1, A2 — decidable and decided on the current shape |

---

## 1. Why this exists

The gate's own rationale, and it is the design driver for everything below:
agents reach for quick-win debugging fixes that prove nothing, so the same bug
recurs. The discipline forces competing hypotheses with A/B/C/D arms, each with
its own success **and** failure conditions and the specific numbers to look for,
run across multiple cases.

**The target is not finding bugs faster. It is making a coincidental fix unable
to pass.**

A fix passes coincidentally when the evidence that "confirms" it would have
confirmed something else just as well. The only structural defence is that the
competing hypotheses predict *different observations*, so that one run of one
test has to kill at least one of them. Everything in this proposal is in service
of making that property **decidable by a machine that cannot read English**,
because `GATE-D2-14` declares `model_judge_in_verdict_path: false` and a gate
that decides "these predictions are different" by reading two paragraphs has a
judge in it under another name.

## 2. What is wrong with the current shape

`contract.md` §7.4 states the record as:

```yaml
hypothesis_id: H-001
claim: "..."
supporting_evidence: []
contradicting_evidence: []
discriminating_tests: []
expected_observations: []
confidence: "unknown|low|medium|high"
status: "open|supported|refuted|inconclusive"
```

Three defects, in increasing order of seriousness.

**(a) The template ships empty.** Every list is `[]`. A check that reads
`all_eight_fields_present` as "the eight keys are present" passes a verbatim copy
of this block. This one is *not* a schema problem — `d2_14_a2` closes it today by
requiring presence **and** non-emptiness, and its negative control is this exact
block parsed out of `contract.md` at run time. No amendment needed. It is
recorded here because the same trap reappears at (c) one level down: typed fields
that are present but vacuous.

**(b) `discriminating_tests` is an untyped list.** In the only recorded
hypothesis set in the repository — `evidence/FINDING-008-implementer-channel-rate.json`
— the field appears (misspelled singular, `discriminating_test`) as a prose
sentence: `"12/12 success at 15s spacing; 503 when issued back to back"`. There is
no test id, no runnable reference, no command, no exit status, no artifact hash.
`test_present_and_executed` cannot be decided from that. "Executed" is not a
property of a sentence.

**(c) `expected_observations` is free text, and is absent from every record.**
This is the one that matters. Over free text the only decidable notion of
distinctness is **string inequality** — and string inequality passes five
restatements of one theory:

```
"the channel rate-limits closely-spaced requests"
"requests issued too close together are throttled by the channel"
"the channel refuses calls that arrive back to back"
"spacing the calls out avoids the failure, so it is a rate limit"
"a per-channel rate limiter rejects bursts on this deployment"
```

Five distinct strings. One theory. A string-inequality check reports all ten
pairs distinct and green-lights a "discriminating test" that discriminates
nothing. `d2_14_a3` runs precisely this input through
`string_distinct_predictions` and records the result in its negative-control
transcript, so the claim that the naive check is inert is demonstrated rather
than asserted. **That is why no string-inequality check was shipped.**

## 3. The proposed shape

### 3.1 `expected_observations` becomes a list of typed predictions

```yaml
expected_observations:
  - observable_id: raw_call_success_rate   # names WHAT is measured
    comparator: ">="                       # one of  <  <=  ==  !=  >=  >
    value: 1.0                             # a number, not a description
    unit: ratio                            # what the number is in
```

Four fields, all required, no free text anywhere in the verdict path. The prose
that a human wants belongs in `claim`, which is not read by any predicate.

### 3.2 `discriminating_tests` becomes a list of typed tests

```yaml
discriminating_tests:
  - test_id: DT-008-spacing
    runnable_ref: "tests/integration/test_channel_spacing.py::test_15s_spacing"
    predicts_if_true:
      - observable_id: raw_call_success_rate
        comparator: ">="
        value: 1.0
        unit: ratio
    predicts_if_false:
      - observable_id: raw_call_success_rate
        comparator: "<"
        value: 1.0
        unit: ratio
```

`runnable_ref` is what makes "executed" decidable: a pytest node id, a script
path, or an evidence-artifact hash — something a runner can execute or a reader
can re-run, resolvable against the repository at gate time.
`predicts_if_true` / `predicts_if_false` are what make the test a *test*: a
discriminating test that predicts the same thing under both outcomes is not
discriminating, and with this shape that sentence is an assertion, not a remark.

### 3.3 One new field on the record: the selection's provenance

A4 asks for `selection_linked_to_test_result`. **§7.4's template has no field in
which to record such a link** — no `selected_because`, no observation reference,
no test identifier. That is the whole reason A4 is staged: the assertion asks for
provenance the contract never gave a slot for.

```yaml
selected_because:
  test_id: DT-008-spacing          # must name a test_id in this record's discriminating_tests
  observed:
    observable_id: raw_call_success_rate
    comparator: "=="
    value: 1.0
    unit: ratio
  observation_ref: "evidence/FINDING-008-implementer-channel-rate.json#/measurements/spaced_15s"
```

This makes the ninth field of the record and it is the only structural addition.
Everything else is a retyping of two existing fields.

## 4. How pairwise distinctness becomes arithmetic

This is the concrete payoff, and the predicate is **already implemented and
already tested** — `src/evaluation/checks_d2_14.py`:
`predictions_are_incompatible` and `pairwise_discriminating`, exercised on worked
examples inside `d2_14_a3`'s evidence and pinned by
`tests/unit/test_checks_d2_14.py`. Nothing below is speculative; it runs today,
on synthetic input, because the real input does not exist yet.

**Step 1 — a typed prediction is a set of reals.** Each
`(comparator, value)` denotes a subset of ℝ:

| comparator | set |
|---|---|
| `< v`  | `(-∞, v)` |
| `<= v` | `(-∞, v]` |
| `> v`  | `(v, ∞)` |
| `>= v` | `[v, ∞)` |
| `== v` | `[v, v]` |
| `!= v` | `ℝ \ {v}` |

**Step 2 — two predictions are incompatible iff their intersection is empty.**
Fold both constraints into one interval `(lo, hi)` with open/closed flags and a
set of excluded points. The intersection is empty iff `lo > hi`, or `lo == hi`
with either bound open, or `lo == hi` and that point is excluded. (An excluded
point cannot empty a non-degenerate interval: the reals are dense.) Predictions
on different `observable_id`s are never incompatible — two statements about
different things cannot contradict each other. Predictions on the same
`observable_id` in different `unit`s are **also** reported as not incompatible,
and that mismatch is itself a finding: a comparison across units is not a
comparison.

**Step 3 — a hypothesis set is pairwise discriminating iff every pair has at
least one incompatible pair of predictions.** Then no single run of the
discriminating test can be consistent with both members of any pair, so every
run kills at least one hypothesis per pair. That is the property that makes a
coincidental fix unable to pass, and at this point it is a `for` loop over
interval arithmetic.

Worked, from `FINDING-008`'s real situation:

```
H-002 "the codex-pro channel is dead"     -> raw_call_success_rate == 0.0 ratio
H-006 "the channel rate-limits bursts"    -> raw_call_success_rate >= 1.0 ratio
```

`[0.0, 0.0] ∩ [1.0, ∞) = ∅` → **discriminated.** One run of three raw calls
settles it, and the transcript states which number decided it and why.

Against a set that only *looks* like two hypotheses:

```
H-006  "the channel rate-limits bursts"          -> raw_call_success_rate >  0.90 ratio
H-006' "a per-channel throttle rejects bursts"   -> raw_call_success_rate >= 0.95 ratio
```

`(0.90, ∞) ∩ [0.95, ∞) = [0.95, ∞) ≠ ∅` → **not discriminated.** Any measurement
at or above 0.95 supports both, so the "test" cannot separate them. This is the
restatement problem caught arithmetically — the same input string inequality
waves through, refused by a comparison of two numbers.

## 5. Exactly what each assertion gains

| Assertion | Today | After |
|---|---|---|
| **A1** `count >= 2` | **decidable, PASS.** Every recorded set is counted against the pack minimum; a one-hypothesis and a zero-hypothesis set are both refused. Limit: no module emits `efah.hypothesis`, so a debugging session that recorded nothing is invisible. | unchanged; §6 would close the coverage limit |
| **A2** `all_eight_fields_present` | **decidable, FAIL.** Presence **and** non-emptiness; the verbatim template is the negative control and is refused on all four lists plus `claim`. | still decidable; gains 2 typed fields to check the *shape* of, not just the emptiness |
| **A3** `test_present_and_executed` | **UNVERIFIABLE.** "Present" is A2's finding; "executed" has no runnable reference to follow; "distinguishes" is undecidable over free text. | **decidable.** `runnable_ref` resolves or it does not; `pairwise_discriminating` decides distinctness arithmetically |
| **A4** `selection_linked_to_test_result` | **UNVERIFIABLE.** The decidable half holds (one selection, not first-recorded, no alternative left open, statuses in enum) but there is no field to record a link in. | **decidable.** `selected_because.test_id` must name a test in the same record; `observed` must satisfy the selected hypothesis's own `expected_observations` and violate at least one competitor's |

## 6. Recommended alongside, not required by this amendment

**No `three_day_plan` item owns `competing_hypotheses_and_discriminating_test`.**
That is the structural reason the gate was never built: `contract.yaml`'s plan is
what the compiler turns into tasks and allowed paths, so an acceptance check
nobody's plan item owns has no task that would have produced it. Adding the typed
schema without adding a plan item that owns the check leaves the same hole in a
better-shaped record. A plan item should also own a producer — no module in
`src/` emits an `efah.hypothesis` record today; the compiler declares the
eight-field schema as an `efah.artifact_schema` object and **nothing validates an
instance against it.** Until something does, A1's denominator stays unknowable:
the gate can only measure the hypothesis sets somebody chose to write down.

## 7. Migration

One artifact carries hypotheses: `evidence/FINDING-008-implementer-channel-rate.json`,
six records. It is a builder-writable evidence file, not pack content, so
retyping it needs no amendment — only the schema it is retyped *against* does.
Its `status: supported_then_refined` on H-001 is outside §7.4's enum
(`open|supported|refuted|inconclusive`) and is reported by `d2_14_a2` today; if
the owner intends that state to exist, the enum is a third thing this amendment
should settle, and if not, H-001 is `refuted` and the refinement belongs in
`claim`.
