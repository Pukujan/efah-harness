# HANDOFF-003 — session handoff for the EFAH build

**Written:** 2026-08-02 · **Contract:** EFAH-CONTRACT-001 **v1.1**
**Supersedes:** HANDOFF-002 (still accurate on environment; superseded on state)

`model-policy.yaml → session_policy` sets `chat_transcript_as_project_memory:
forbidden`. This file, git history, TerminusDB and the checkpoint store are the
project memory. Re-ground from those.

---

## Where things stand

**Branch:** `feat/kernel-and-ci` · **PR #2** open, blocked · **`main`:** preflight only.

| | |
|---|---|
| Tests | **1064 pass**, 11 skipped |
| Gate run | **PASS=5, FAIL=0**, 22 UNVERIFIABLE (16 not-yet-executable) |
| Assertion hashes | 27 gates, **0 violations** |
| Availability probe | **14–15 / 15** (implementer flaps, see FINDING-008) |
| Sealed holdouts | **minted, kill_rate 1.0**, verified sound |

**Delivery priority (DEC-005, binding):** items 1–3 ✅ done. Item 4 in progress.

---

## Read these four first

1. **DEC-008** — *a failure is a configuration finding, not a verdict.* The most
   important rule in the project and the one this builder broke repeatedly.
2. **DEC-007** — owner answered FINDING-005 with **option D**, backed by their own
   benchmark. Holdout generation is unblocked.
3. **FINDING-009** — kept deliberately as a **corrected wrong conclusion**.
4. **BUILD_VS_INTEGRATE-001** — chat front end: integrate Open WebUI, do not build.

---

## What changed this session

- **DEC-006 option B built and measured 6/6.** Separate `efah-verifier` uid, `0700`
  store the kernel refuses to the builder, root-owned generator, opaque receipt seam.
- **Sealed holdouts actually work.** Baseline passes on correct code, each mutant
  installed as `subject.py` and killed individually. Three earlier runs reported a
  perfect score while testing *nothing*.
- **§27 evidence package** — 21/22 fields; the 22nd (`auto_merged_pr_reference`)
  cannot exist before the merge this gate gates.
- **§7.3 citation enforcement** (FINDING-007) — a citation records a quote; the
  validator re-reads the source and checks the quote is there. Wired into tier
  promotion: nothing rises above T2 without a `SUPPORTED` verdict.
- **FINDING-006** — 16 contract-required separation edges now enforced from the
  contract text; 8 previously had no rule at all.
- **Owner instruction consumer** — the chat surface now drives real work, verified
  end to end via systemd.
- **OpenAI-compatible façade** — five modes as models, mounted, `/v1/models` live.
- **Model map rebuilt** — every flash/lite model retired, 8 vendor families.
- **Streaming is now the default everywhere.**

---

## The one lesson that cost the most time

**Non-streaming requests die on long generations.** The owner's cortex research
(`STREAMING-DISPATCH-FINDING-2026-07-19`) established it: an idle connection is
killed by the edge proxy at ~100–120s. They saw HTTP 524; this harness reproduced
it as **408** at 8000/4000 tokens and **502** at 16000 — after raising our own
timeout to 300s and proving it was not the binding constraint.

I concluded from that that two models "cannot do long-form generation". **Both
were wrong.** With streaming, `kimi-k2.7-code` — written off after *one* empty
response — emits 14,587 characters.

> **One success proves the model can do the task. One failure proves nothing.**

`tools/fuzz_generation_config.py` sweeps `max_tokens × stream × task_size` and
stops at the first working cell. **Run it before recording any model verdict.**

---

## OPEN — what the next session must pick up

### 1. Owner actions (blocked on the owner, not on you)

- **kimi-k3 / `[官4][量] glm-5.2`** — both return **400, not configured** on the eval
  gateway. Needs a Railway config entry **and** the `sitecustomize.py` CJK patch
  from the owner's `CONFIGURATION-GUIDE.md` trap #5 — `官` is CJK and **billing
  happens before the header crash**.
- **Open WebUI** — install decision outstanding. Façade is built and mounted.
- **GitHub sealed repo + second App** — for the DEC-006 option-A migration. The
  owner must create both; the builder must **not** have access or GATE-D1-08 A1
  fails by construction. Would make A1 and A3 executable and close DEBT-001.

### 2. Not every model is verified for long-form work

Only `claude-opus-4-8` and `kimi-k2.7-code` are proven on the real generation
task. The other 13 are verified by **short** probes only. Sweep before trusting.

### 3. `sealed_holdout_author` is still `claude-opus-4-8`

`claude-opus-5-thinking` was tried and reverted — **on a wrong conclusion**, before
streaming was fixed. It measured 8/8 with tool calls at p50 2.4s and resolves to
**channel 263**, not the `kiro-pro` 234 that three assurance roles share. **Retry it
now that streaming works.** Likely a straight win on capability *and* transport.

### 4. FINDING-008 — implementer channel flaps

`gpt-5.6-luna` 503s in bursts, 12/12 at 15s spacing. Probe paces at 1.5s and it
still flaps. Needs per-channel spacing, not just the account floor.

### 5. Remaining contract work

- 16 gates `NOT_YET_EXECUTABLE`.
- GATE-D3-25 needs a real green PR merged by CI.
- `strict` is off on branch protection, so `branch_up_to_date` must be enforced by
  the harness, not assumed.
- §15 retrieval planes are **unbuilt** — FINDING-007 delivered enforcement without
  retrieval.

---

## Environment

```bash
PY=/home/yoav/efah/.venv/bin/python
cd /home/yoav/efah/efah-harness
set -a && . ~/.efah/env && set +a          # PLANE_API_KEY etc — tests fail without it
$PY -m pytest tests/ -q                    # ~2.5 min
$PY tools/check_assertion_hashes.py
PYTHONPATH=src $PY -m evaluation.gate_runner --json evidence/gate-run-summary.json
PYTHONPATH=src $PY tools/gate_dec_006.py   # slow: triggers a real generation
PYTHONPATH=src $PY tools/fuzz_generation_config.py <model>
PYTHONPATH=src $PY tools/build_evidence_package.py --run-tests
```

**Services:** `efah-owner-surface`, `efah-instruction-consumer` (both user units,
`Restart=always`, both unset Anthropic credentials on start).

**Surface:** `http://gravebuster.tail733a0f.ts.net:8088/owner/` — **hostname, not IP.**

**Verifier identity:** `efah-verifier`, store `/var/lib/efah-verifier` (0700),
generator `/opt/efah-verifier/bin/generate-holdouts` (root-owned), test runner
`/opt/efah-verifier/venv` (root-owned), shared throttle
`/var/lib/efah-throttle/state.json`.

---

## Standing corrections earned the hard way

Everything in HANDOFF-002's list still holds. Added this session:

- **A missing tool exits like a failing test.** `python -m pytest` without pytest
  exits **1**, identical to a test failure. That produced a fabricated
  `kill_rate: 1.0` on a set that ran nothing. Assert the runner exists *before*
  any verdict depends on it.
- **A probe measures the request it makes.** Short probes passed while the real
  task failed — three times, three different causes. If the probe's shape differs
  from production, it is evidence about the probe.
- **Absence is not success.** A `None` citation verdict blocks promotion; a
  `stat` denied is recorded as `stat_denied`, never as `exists: false`.
- **Re-run the board before committing.** I committed twice without doing so and
  turned it red twice.
- **The compiler is stricter than my own tables, and was right.** It caught
  `mutant_author` sharing a family with the implementer when
  `models.separation` had that pair at agent level only.
- **A blocker can be answered by accident.** `ANSWER_BLOCKER` fell back to
  `blockers[0]` and accepted any text — the word "Hello" closed an
  `OWNER_RISK_ACCEPTANCE` decision. Fixed; the lesson is that convenience
  defaults on authority paths are how governance fails quietly.

---

## Kickoff for the next session

```
Read HANDOFF-003.md at the repo root, then re-ground from project-pack
(contract v1.1 = v1.0 + AMENDMENT-001) and from git log. Do not rely on any
conversation history.

Continue the EFAH build under contract EFAH-CONTRACT-001 v1.1. DEC-001, DEC-002,
DEC-005, DEC-006, DEC-007 and DEC-008 are signed and bind you. delivery_priority
is binding and may not be reordered.

READ DEC-008 FIRST. A model failure is a configuration finding, not a verdict.
Run tools/fuzz_generation_config.py before recording any model as failing.
Streaming is the default on every path; a non-streamed long generation dies at
the proxy, not at the model.

First task: retry sealed_holdout_author on claude-opus-5-thinking now that
streaming works — it was reverted on a conclusion that has since been corrected.
Then the §15 retrieval planes, which FINDING-007 left unbuilt.

Continue autonomously through repair, verification, PR and merge whenever the
auto-merge gates pass. Stop only at VERIFIED_COMPLETE, a permitted typed blocker,
FAILED_CONTRACT, FAILED_ASSURANCE, or CANCELED.
```
