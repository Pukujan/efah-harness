# PLAN-001 — What to build, in what order, and what is still unverified

**Status:** DRAFT · 2026-08-03 · supersedes nothing; nothing here is approved.

**Reading rule for this document.** Every item is tagged with how well it is
established. Tonight four claims of mine were sent for independent verification
and **three came back refuted**. So this plan separates *verified* from *asserted*
rather than presenting a flat list, because a flat list is how the last four
rebuilds started.

| Tag | Meaning |
|---|---|
| **VERIFIED** | Independently checked against code this session |
| **RESEARCHED** | An agent produced a concrete change set with file:line |
| **UNDER TEST** | Verification in flight; do not act yet |
| **REFUTED** | I proposed it, research killed it. Recorded so it is not proposed again |
| **BLOCKED** | Needs an owner decision or an owner action |

---

## 0. Where the harness actually is

**Built and working** — contract compiler (1,664 objects, 91 requirements, 57
tasks, 870 edges, 0 cycles), three minted deterministic oracles with zero
surviving mutants, gate runner at **90/127 assertions and 11 gates PASS**, the
walking skeleton at **14 of 15 stations**, drift engine, requirements graph,
blinding with measured protected-instance isolation, task ledger, lease fencing,
12 LangGraph graphs, a 10-module middleware stack.

**Not built** — nothing writes code; no sandbox; no worktree; retrieval planes
(§15) absent; no eval task; no SDD/TDD; hypothesis gate `NOT_IMPLEMENTED`;
holdout execution `UNAVAILABLE`.

**The honest summary: the governance half works and the execution half does not
exist.** The harness can judge work it cannot yet do.

---

## 1. Track A — safe now, no decision required

### A1. Inspect AI first eval task — **RESEARCHED, in flight**
§14.5 forbids deferring the Eval Lab until after the application modules.
Additive files only; deterministic lane checker returns the verdict, Inspect
attaches its own score under `diagnostics` only.
**Constraint:** use a mutation-calibrated lane. Only **19 of 71** qualify, and
`objective_cruxeval` is disqualified — its checker reads truth from stdout, so a
candidate can print a marker and forge its own ground truth.

### A2. Scorer blind-spot repairs — **VERIFIED**
Two forgeable checkers, same bug class, both found by execution on 2026-08-03:
- `objective_cruxeval` — truth read from stdout.
- `objective_ssrf_path_traversal_behavioral` — `ALLOWED_BODY` is a fixed
  constant a candidate can return without fetching anything.
**The fix already exists in the codebase:** the path-traversal sibling is immune
because its in-root body carries a fresh per-run random suffix. Apply the same
nonce. The general rule — *any checker whose expected value is a constant the
candidate knows is forgeable* — belongs in the checker-core contract.

### A3. `uv` lockfile + `pyproject.toml` corrections — **RESEARCHED**
`pin_versions: true` is unenforceable today: no lockfile exists anywhere.
**Fix the declaration first** — `starlette` is imported by 11 modules and
undeclared; `langchain-core` and `anthropic` likewise; `pydantic-settings`,
`PyJWT`, `cryptography` are declared with zero import sites; pytest/ruff/build
are installed and declared nowhere. Locking a wrong declaration freezes the
wrong thing.
Then `uv` — the only candidate that stays **out** of the closure it locks, and
whose resolver enforces the `click<8.2.2` ceiling that `inspect_ai` alone
imposes and that `pip freeze` structurally cannot.
**Do not overstate the win:** a lock closes roughly **1.5 of 9** §16.3 fields,
Python only. `importer.py:289` hardcodes `lockfile_source` to the policy file
itself while reading versions from `selected_stack`, where every entry is
`TODO_builder_probe` — a file citing itself for versions it does not contain.
Fixing that line matters more than the lock.
**Note:** asking the owner about dependency versions is off-contract —
`contract.md:1807` requires inspection, and `autonomy-policy.yaml` lists them
under `must_not_ask_about`.

### A4. Citation validator hardening — **RESEARCHED, and do not reorder this**
`research/claims.py` currently returns `SUPPORTED` at tier `T4` for a flatly
false load-bearing statement citing `quote="the"` with a nonexistent location.
`exact_location` is never checked and is echoed back as if confirmed;
`load_bearing=False` with no citations passes; a `MALFORMED` citation alongside
a good one does not block; pointers are not repo-constrained.
Fix order: minimum quote substantiveness and statement/quote linkage → enforce
`exact_location` → close the two bypasses → hash raw bytes → constrain pointers.
**Relocation comes last.** Moving it to `governance/` while it behaves this way
would install a rubber stamp on the deterministic verdict path — and it would
pass `prove_no_judge` while doing so.

### A5. GATE-D2-14 hypothesis gate — **BUILT, two assertions staged**
All four now execute (`src/evaluation/checks_d2_14.py`,
`tests/unit/test_checks_d2_14.py`). **No `three_day_plan` item owns it** — that
is unchanged, and it is still the structural reason it was never built. A plan
item must own the acceptance check, or it will not stay built.

Both traps were held to, and each one shaped a verdict:
- The contract ships the template with every list empty, so a naive
  `all_eight_fields_present` check **passes a verbatim copy of the template**.
  A2 checks presence *with* non-emptiness, and its negative control is that
  template parsed out of `contract.md` at run time. A2 **FAILS** on the real
  records: `evidence/FINDING-008-implementer-channel-rate.json` carries four
  keys per record, `expected_observations` is absent from all six, and one
  `status` is outside the contract's enum.
- Pairwise-distinct predictions is **not decidable on the current shape**, so
  no string-inequality check was shipped: A3's transcript shows string
  inequality reporting five restatements of one theory as five distinct
  hypotheses. A3 and A4 are `UNVERIFIABLE` with stated reasons; the typed shape
  they need, and the arithmetic predicate that decides distinctness once it
  lands, are in
  `docs/decisions/PROPOSED-AMENDMENT-001-typed-hypothesis-records.md`.
  A1 passes: every recorded set holds at least the pack's minimum of two.

---

## 2. Track B — Open WebUI

### B1. Harden it — **RESEARCHED, in flight, ~1 hour, low risk**
Turn off `code_execution`, `code_interpreter`, `memories`, `arena`; pin the
image by digest `sha256:6a773e5c…`; add the missing `chat_front_end` entry to
`dependency-policy.yaml`.
`code_interpreter` is currently the **only general-purpose code execution in the
deployed system** — browser-side pyodide, outside every gate. `memories`
violates `session_policy: chat_transcript_as_project_memory: forbidden`.
Requires one container recreate; the data volume must be preserved; a `webui.db`
backup and a rollback command are part of the change set.

### B2. Put the façade inside the middleware stack — **REFUTED as described**
I called this "one edit". It is not, and as stated it takes the surface down:
- **No EFAH token is configured on the live service**, so `any_configured()` is
  False and auth refuses everything — including `/owner/`, which is an HTML page
  a phone browser loads with no `Authorization` header.
- `/healthz` and `/owner/health` are not in `PUBLIC_PATHS`, so **walking-skeleton
  station 15 flips to FAILED and `project_state` becomes `FAILED_ASSURANCE`**.
- `UntrustedContentMiddleware` denylists the bare string **`"6364"`** against the
  whole body. The OpenAI protocol resends the entire conversation each turn, so
  once that string enters a thread — a port, a hash fragment — **every later turn
  403s forever** and the conversation is unrecoverable.
- `create_app` unconditionally mounts `contract_router`, publishing an
  **in-memory** control plane on the tailnet.
**Real cost: 4–6 days, gated on two design decisions** — browser auth for
`/owner/`, and a per-route posture for `UntrustedContentMiddleware` so free text
is not denylisted while command routes stay protected.

### B3. Wire `graph_id` so chat reaches the planner — **REFUTED**
`planning_graph`'s first node binds `state` and never reads it; it calls
`decompose(services.pack)`. **There is no model call in that graph.** Wiring
`efah-plan` to it returns the same pack-derived list for every message — a
regression from today, where the mode at least routes to a real planner model.
Nothing reads `Mode.graph_id` at all, so correcting the three wrong strings is a
no-op on dead data (worth doing, with a test, purely to stop the drift).
Making the graphs accept a free-text goal is **5–7 days** and gated on a §9.4
contract question: an owner's ad-hoc goal *is* an `UNLINKED_TASK` by the current
definition.

---

## 3. Track C — measurement

### C1. Cross-vendor vs single-model — **PREREG-001 written, awaiting five values**
Arms A / A′ / B / C / D. Anthropic appears in no arm. Tasks and prompts come
from third-party benchmarks verbatim. Seats rotate. Execution decides; no judge.
`n_per_arm` is computed from pilot variance, never chosen — the precedent being
A1, which registered N=59–106, ran at **n=3**, and reported PASS.
**Arm B is the load-bearing arm:** it isolates role separation from vendor
diversity, and **C−B is the vendor contribution.**

### C2. The cheaper experiment that should run first — **VERIFIED as a design**
"Is M30 load-bearing?" was answered by convening a model panel. It did not need
one. The ratchet is red (61 orphans vs a 37 baseline) and the code shipped
anyway — that is already a measurement. Seed a deliberate orphan, confirm the
ratchet catches it, then observe whether red changes anyone's behaviour.
Produce → mutants → oracle. No model call, and it answers the question the panel
only had opinions about.

### C3. The methodology panel — **downgraded, not cancelled**
Two defects, both mine: `strong_agreement` is a **voting rule** and I quoted it
as authority, and all three families received **identical input text**, so their
agreement is correlated by construction. There is no ground truth to score them
against, no holdout, and no graybox boundary.
**What survives:** the self-preference measurement (same stimulus, different
families, observable differential — a comparison, which survives the correlated
-input problem that absolute rankings do not) and the critics' *reasoning* as a
source of hypotheses to test deterministically. The **ranks are not findings.**

---

## 4. Track D — blocked on the owner

| Item | What is needed |
|---|---|
| **Owner blocker Q1** | The sealed verifier endpoint. §17.2 keeps it out of build-side config deliberately; the harness cannot self-serve it. **This is the only thing between the walking skeleton and a terminal project state** |
| **llama-index** | A §14.2 record. It hard-requires `llama-index-workflows`, which ships a top-level `workflows` package that shadows EFAH's own. `rag_components` is `replaceable: false`, so substitution needs a recorded decision |
| **A coding mode** | `USING-EFAH.md:222` — a `code` mode conflicts with §21.2 by design, since the ceremony *is* the gate. Note this is about *ungated direct editing*; gated code execution inside the work-unit path needs no such decision, only building |
| **PREREG-001 §7** | Five values: eligible lanes, sample seed, `min_lift`, `cost_tolerance`, and the A/A noise band |

---

## 5. Under test right now

**Is role separation mechanically enforced, or only promised?** I claimed the
seat map works by *information asymmetry* — the test author cannot fit a test to
code it has never seen — and that this survives the Knight-Leveson critique that
defeats voting. **That is only true if a seat cannot read what it should not.**

Reasons to doubt it, already on record: `methodology_receipt.independence_errors`
compares self-declared **seat name strings** with nothing cross-checking a
dispatch log; the single receipt on disk names `test_author: sol` and
`holdout: terra`, **both openai**, while asserting they are distinct;
`seating.assert_independent` has zero production callers; M29's access matrix is
self-described as discipline-only; and PR #29 records a builder that **read the
holdout and tuned its probe to match**.

If the asymmetry turns out to be procedural rather than mechanical, then Arm C
of PREREG-001 is not testing what it claims to test, and the fix is a mechanism
before the experiment. **Do not start C1 until this returns.**

---

## 6. Suggested order

1. **A2** (scorer repairs) — everything measured downstream depends on the
   checkers being honest.
2. **B1** (Open WebUI hardening) — closes live ungated code execution.
3. **A3** (declaration + lock) — stops the `click` class of silent breakage.
4. **A1** (first Inspect task) — §14.5 says this cannot wait.
5. **A4** (citation validator) — then, and only then, relocate.
6. **A5** (hypothesis gate) — with a plan item that owns it.
7. **C2** (the cheap M30 experiment).
8. **C1** (PREREG-001) — *after* §5 returns.

Track B2/B3 and Track D stay parked until the decisions land.
