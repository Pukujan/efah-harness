# PREREG-001 — Does cross-vendor multi-role construction beat one model?

**Status:** DRAFT, awaiting owner approval. Nothing runs until this is frozen.
**Raised:** 2026-08-03 · **Prompted by:** the owner asking how to measure the
question, and objecting — correctly — that a ranking produced by a single
Anthropic-family agent had been reported as a finding.

---

## 0. The question, stated so it can fail

Does building a work unit through the multi-role cross-vendor pipeline
(producer · visible-test author · sealed-holdout author · reviewer, on
different vendor families) produce **more code that passes tests it never saw**
than one model doing every role?

Four years and four repositories have assumed yes. It has never been measured.
The honest null hypothesis is that it makes no difference, and the second most
likely outcome is that the *ceremony* helps and the *vendor diversity* adds
nothing on top.

---

## 1. Arms — and the one that makes the result interpretable

Two factors, fully crossed. Ceremony ∈ {none, full}; vendor ∈ {single, cross}.

| Arm | Vendors | Ceremony | What it isolates |
|---|---|---|---|
| **A** | single | none | Baseline. One model, one shot, no separate test author or reviewer |
| **B** | single | full | **The arm everyone skips.** One model plays every seat |
| **C** | cross | full | The full stack under test |
| **D** | cross | none | N models, majority vote, no roles |
| **A′** | *identical to A* | *identical to A* | **A/A null control** — see §5 |

**Why B is mandatory.** If C beats A, and B also beats A by the same margin,
the benefit is the ceremony and vendor diversity bought nothing. Only the C−B
gap is the vendor effect. Without B the experiment produces a story.

`cortex_core/pack_experiment.py:229-231` already enforces that the baseline arm
is present in any registered prereg. This satisfies it.

---

## 2. Removing the experimenter's bias — structurally, not by promise

The assistant designing this is Anthropic-family, and the prior being tested was
Anthropic-authored. Bias can enter at exactly four points. Each is closed by
construction rather than by care.

| Entry point | Closure |
|---|---|
| **Scoring** | Hidden-test execution decides. **No model judge anywhere in the verdict path.** Inspect AI runs and logs; the lane's own deterministic checker returns the verdict, per the rule already written at `evals/oracle_adapter.py` — an external framework's score attaches as advisory `diagnostics`, never as `passed` |
| **Model choice** | **Anthropic does not appear in any arm.** Seats are drawn only from openai, xai, deepseek, qwen, zhipu, moonshot. The designer's vendor is not a participant |
| **Task selection** | No hand-picking. Every task in the selected lanes, or a seeded random sample with the seed frozen in §7 below. Task provenance is third-party (MBPP, BigCodeBench), not ours |
| **Prompt authoring** | Each benchmark's **canonical prompt, verbatim**. Zero prompt authoring by the designer — a prompt written in one model's idiom silently favours models trained like it |
| **Seat assignment** | Counterbalanced rotation: every family plays every seat across the task set. Otherwise "this vendor implements well" masquerades as "cross-vendor works" |

---

## 3. Outcome measures

**Primary — pre-registered, single, binary.**
`hidden_test_pass` — the candidate passes the lane's hidden test block, which
was never in the builder's context. Pure subprocess execution.

**Secondary, all deterministic, reported but not gating.**

- `visible_test_mutation_kill_rate` — seed mutants into the candidate and count
  how many the arm's *own authored tests* kill. Asks whether cross-vendor
  test-authoring produces tests with teeth, which is a different question from
  whether the code is right.
- `tokens_total` and `wall_clock_seconds` per task. A 3× cost for a 4-point gain
  is a real and reportable finding.
- `recurrence` — the candidate is re-run against a held-back *variant* of the
  task. This is the owner's actual concern: a fix that passes once because the
  bug was intermittent.

---

## 4. Scorer validation — before, not after

**The experiment cannot be trusted further than its checker.** Measured on
2026-08-03: `objective_cruxeval`'s checker reads truth from **stdout**, so a
candidate that prints a marker and exits forges its own ground truth;
`objective_ssrf_path_traversal_behavioral`'s legit-fetch invariant compares
against a **fixed constant** a candidate can return without fetching anything.
An experiment scored on a forgeable checker measures the forgery.

Therefore:

1. **Only lanes with recorded mutation-kill calibration are eligible.** Of 71
   lanes, 19 currently qualify; the rest record `insufficient_controls`.
2. Any lane added must first pass a blind-spot sweep — authored near-miss pairs,
   with the checker required to discriminate.
3. The eligible-lane list is frozen in §7 and may not grow after data collection
   begins.

---

## 5. The A/A null control — the instrument's own negative control

**Arm A′ is byte-identical to arm A and is labelled differently.**

If the harness reports a difference between two identical arms, the harness is
broken and every other number in the run is noise. This is the negative control
for the *instrument* rather than for the hypothesis, and it costs one extra arm
at pilot scale.

`pack_experiment.py:288-292` already blinds cell labels, so the scorer cannot
tell A from A′.

**Kill rule:** if |A − A′| exceeds the pre-registered noise band (§7), the run is
void. Not adjusted, not explained — void, and the harness is fixed first.

---

## 6. Reliability

- **k = 3 repetitions** per task per arm, fixed seeds. A point estimate without
  a variance estimate cannot distinguish a real 5-point gap from run-to-run
  noise.
- **Model versions pinned and re-probed at start and end.** If a model changed
  mid-run that is a finding, not a footnote — `implementer-i12` moved from 2.8 s
  to 44.3 s median latency inside one night on 2026-08-02/03.
- **Task order randomised per arm**, seed recorded.
- **One code path; the arm is a parameter.** A harness bug then hits every arm
  equally instead of favouring one.
- **The harness is adversarially reviewed by a non-Anthropic seat before the
  run.** This is the correct use of a model critic: reviewing the instrument,
  never deciding the outcome.

---

## 7. To be frozen at approval — the owner fills these

These are the values `pack_experiment.preregister()` hashes. Once frozen, any
edit changes `prereg_sha256` and `score_experiment` returns `PREREG_VIOLATION`.

```yaml
arms:            [A, A_prime, B, C, D]
baseline_arm:    A
families:        [openai, xai, deepseek, qwen, zhipu, moonshot]   # no anthropic
eligible_lanes:  TODO_owner        # from the 19 mutation-calibrated lanes
task_selection:  all | seeded_sample
sample_seed:     TODO_owner
n_per_arm:       TODO_from_pilot   # computed from pilot variance, not guessed
k_repetitions:   3
primary_metric:  hidden_test_pass
min_lift:        TODO_owner        # the difference worth acting on
cost_tolerance:  TODO_owner
min_cell_n:      TODO_from_pilot
aa_noise_band:   TODO_from_pilot   # |A - A_prime| above this voids the run
kill_rule:       "C - B <= min_lift  =>  vendor diversity is not carrying its cost"
```

**`n_per_arm` is computed, never chosen.** For a binary outcome near a 50% base
rate, detecting a 20-point difference at 80% power needs roughly 100 tasks per
arm; 10 points needs roughly 400. `evals/ab_cortex_scaffold/power.py` builds the
grid from the pilot's observed variance.

The precedent this exists to avoid: `evals/reports/A1-preregistration-2026-07-14.json`
registered N = 59–106 and was run at **n = 3**, all rates zero, and reported
**PASS**.

---

## 8. Stages

1. **Pilot, n = 20, including A′.** Shakes out the harness, proves the checker
   discriminates, and produces the variance estimate §7 needs. Cheap, and the
   A/A result gates everything after it.
2. **Power calculation** from observed pilot variance.
3. **Freeze this document.** `preregister()` refuses to overwrite an existing
   registration, so this is one-way.
4. **Powered run.** Throttle is 90 req/min account-wide, so this is wall-clock
   bound as much as budget bound.

---

## 9. Declared limitations — recorded before the result, not after

- **Benchmark tasks are well-specified. This design probably understates what
  cross-vendor buys.** Ron, Baudry & Monperrus (arXiv 2606.20158, June 2026)
  measured 48 agent-written implementations of one specification: three-version
  majority voting cut mean failures from 387.4 to 131.0, **and** found
  substantial common-mode failure concentrated at *ambiguous* specification
  clauses. Ambiguity is where real work lives and where benchmarks are thinnest.
  A null result here is therefore evidence about benchmark tasks, not about the
  owner's actual workload.
- **`hidden_test_pass` measures code that passes tests, not code that is
  correct.** The hidden block is a sample of behaviour, not a proof.
- **Six families is not six independent samples.** Correlated training data
  means nominal independence overstates real independence; the same 2026 result
  is the evidence. The experiment measures the *effect*, and cannot decompose how
  much of the residual is correlation.
- **Anthropic's exclusion removes a bias and adds a gap.** The result will not
  say whether including an Anthropic seat would have helped. That is the correct
  trade for this run and should be revisited in a follow-up where Anthropic is
  one family among six, counterbalanced.

---

## 10. What a result would license

| Outcome | What it licenses |
|---|---|
| **C − B > min_lift**, A/A clean | Vendor diversity carries its cost. Keep the cross-vendor seat map |
| **C ≈ B**, both > A | The ceremony is doing the work. Keep the roles, drop the vendor requirement and its routing cost |
| **C ≈ B ≈ A** | Neither is carrying its cost on this task class. Do not generalise past well-specified tasks — see §9 |
| **A/A gap > noise band** | Nothing is licensed. Fix the harness and re-run |

No outcome licenses a claim about ambiguous or long-running work. That needs a
different instrument and its own pre-registration.
