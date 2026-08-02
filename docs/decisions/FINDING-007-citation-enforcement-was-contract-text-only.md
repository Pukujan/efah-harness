# FINDING-007 — forced citation was contract text with nothing behind it

**Raised:** 2026-08-02 · **Class:** mechanization gap (§13.4)
**Status:** **CLOSED in code.** The retrieval plane it assumes remains open debt.
**Raised by:** the owner, asking whether "forced RAG and a citation requirement
for every task" was in the package, and asking for confirmation rather than
assurance.
**Evidence:** grep over all of `src/`, recorded below. Measured, not recalled.

## The answer to the question asked

Yes, it is in the contract — in four places:

| Clause | Requirement |
|---|---|
| **§7.3 Source assurance** | *"Every load-bearing claim MUST record"* — 11 fields, including **exact supporting location**, **direct support versus inference**, and **content hash and retrieval provenance** |
| **§15.4 Retrieval pipeline** | ends in *"citation and claim validation"*; *"The retriever MUST be able to return `INSUFFICIENT_EVIDENCE`"* |
| **§15.5 Knowledge tiers** | T0→T7; *"Unverified agent output MUST NOT be presented as trusted knowledge"* |
| **§18 / `methodology-policy.yaml`** | *"'Done' without named evidence is invalid"*; `M-23 citation_discipline`, a `contractual_invariant` |

## What was actually enforced

**Mechanized and passing:** §15.5. `src/knowledge/tiers.py`, GATE-D2-18 **4/4
PASS** — agent output is clamped to ≤T2 whatever it claims, promotion above T4
requires a passing verification from a different vendor family, hard gold needs
all five §15.6 steps, and a deliberately unverified item is refused at T6/T7.

**Not mechanized — measured across `src/`:**

```
source_id | source_class | exact_supporting_location | direct_support
  | retrieval_provenance          → 0 matches
INSUFFICIENT_EVIDENCE             → 0 matches
citation | validate_claim | load_bearing | source_authority
                                  → 1 match, an unrelated comment
hypothesis_id | discriminating_tests
                                  → 0 matches in src/
src/research/                     → __init__.py only
acceptance gates mentioning citation
                                  → none of the 27
```

`M-23 citation_discipline` is **selected** by the applicability compiler for
`research_or_debugging` and for new-dependency work, and **nothing checked that
the task complied**. Selecting a methodology is not enforcing it.

## Why the gap mattered more than it looked

The tier system stops an unverified claim being *presented* as trusted. It does
not stop the claim being **fabricated**, because nothing checked that a cited
source says what the citation says it says. Those are different failures:

- tiers catch *"this was never verified"*;
- nothing caught *"this cites §7.3 for a sentence §7.3 does not contain"*.

The second is the one a model produces without trying to. A fabricated citation
to a **real document** is the realistic failure — plausible source, plausible
section, plausible-sounding quote, and no such sentence anywhere in the file.

## What was built

`src/research/claims.py`. The mechanism is deliberately dumb, because a dumb
check is one that cannot be argued with:

> **A citation records a quote and a location. The validator re-reads the source
> and checks the quote is actually there.**

Deterministic, no model in the verdict path — §17.3's top tier, "exact
deterministic execution/state oracle", applied to citations. Three failure modes
stay separate, because conflating them hides the interesting one:

- **`UNSUPPORTED`** — the quote is not in the source. *The hallucination signal.*
- **`STALE`** — the quote is there but the content hash has changed since
  retrieval. The claim was true and may not be now (§15.7).
- **`UNRESOLVABLE`** — the pointer does not resolve. A deleted file is an
  infrastructure problem, not a lie, and reporting it as one would let a real
  fabrication hide behind a plausible excuse.

A fabricated quote in a source that *also* changed reports `UNSUPPORTED`, not
`STALE` — checked in that order on purpose.

Quote matching normalises **whitespace only**. No case folding, no punctuation
stripping, no fuzzy ratio: `"MUST NOT"` and `"must"` are the difference between
a contract clause and its opposite, so a near-match is not a match.

Claim-level rules, all returning §15.4's `INSUFFICIENT_EVIDENCE` rather than a
false verdict:

- a load-bearing claim with **no citation** → `INSUFFICIENT_EVIDENCE`;
- citations that are **all `MODEL_ASSERTION`** → `INSUFFICIENT_EVIDENCE`, per
  §15.5. A model being quoted accurately is not evidence that it was right;
- citations that are **all `INFERENCE`** → `INSUFFICIENT_EVIDENCE`. Inference is
  permitted and must be labelled, and an `INFERENCE` citation whose
  `inference_step` is empty is structurally malformed — an unstated inference is
  where a false claim hides behind a true source;
- naming an `affected_requirement` makes a claim load-bearing **regardless of
  the `load_bearing` flag**, because marking a load-bearing claim incidental is
  the obvious way around the whole thing.

## Wired into promotion, not left beside it

`knowledge.tiers.evaluate_promotion` now blocks promotion above T2 unless the
item's citation verdict is `SUPPORTED`. **A `None` verdict blocks.** An item
whose citations were never checked is not a passing item — treating an absent
verdict as success is FINDING-004's error again, counting a missing signal as
evidence.

The coupling is by literal string, not import, so the knowledge plane does not
depend on the research plane; a test pins the two together so they cannot drift.

Adding the rule turned three existing GATE-D2-18 tests red, because their
fixtures had no citation verdict. The fixtures were updated and the rule was
not weakened — the standing correction is that a check firing on a guardrail
means fixing the check, never relaxing the gate.

## What is honestly still missing

**There is no retrieval pipeline.** §15.1's seven planes, §15.3's LanceDB row
schema, and §15.4's lexical → dense → fusion → rerank → contradiction sequence
are unbuilt. LanceDB is configured in `environments.yaml` and never used.

So this is **enforcement without retrieval**: the harness can now prove a
citation is real, and cannot yet *find* sources for an agent to cite. That is
the right order — a retrieval pipeline feeding an unenforced citation field
would produce well-sourced-looking output with the same guarantee as none — but
it is half of §15 and is recorded as such rather than described as complete.

**And the residue the check cannot catch:** a model can quote a real source
accurately and still draw a false conclusion from it. Entailment is a judgment,
and §17.5 makes an uncalibrated judge advisory, so it is not in the verdict path.
What is guaranteed is narrower and checkable — the source exists, it is
unchanged, and it contains the words the claim says it contains. The weaker
attack that remains leaves a verifiable trail; the stronger one no longer works.
