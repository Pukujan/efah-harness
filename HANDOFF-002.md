# HANDOFF-002 — session handoff for the EFAH build

**Written:** 2026-08-02 · **Contract:** EFAH-CONTRACT-001 **v1.1**
**Purpose:** let a fresh session continue without reading a conversation.

`model-policy.yaml → session_policy` sets `chat_transcript_as_project_memory:
forbidden`. This file, the git history, TerminusDB, and the checkpoint store are
the project memory. Re-ground from those, not from a transcript.

---

## Where things stand

**Branch:** `feat/kernel-and-ci` (pushed). **PR:** #2, open, `blocked`.
**`main`:** holds the preflight record only — deliberately. Nothing merges while
`hidden_holdout` cannot return PASS, because §21.2 forbids the implementing agent
self-certifying.

| | |
|---|---|
| Tests | **919 pass**, 11 skipped (live-network, opt-in via `EFAH_LIVE_TESTS=1`) |
| Live e2e | **7 pass** against real TerminusDB, LangGraph, LiteLLM, tailnet |
| Gate run | **PASS=5, FAIL=0**, 22 UNVERIFIABLE (18 not-yet-executable) |
| Assertion hashes | 27 gates, **0 violations** — no gate has been weakened |
| Walking skeleton | **16/17 stations exercised**, 0 composition findings |

**Delivery priority (DEC-005, owner, binding — do not reorder):**

1. ✅ complete walking skeleton, end to end, zero placeholders
2. ✅ owner control surface — **GATE-D1-10 PASS**, 10/10
3. ✅ vendor neutrality — **GATE-D1-07 PASS**, 5/5
4. everything else ← **you are here**

---

## The one thing running

**Owner control surface**, `http://100.93.66.35:8088/owner/` — systemd user unit
`efah-owner-surface`, `Restart=always`, linger enabled. Survives reboot.
Credential in `/home/yoav/.efah/surface.env` (0600, `TERMINUSDB_ADMIN_PASS`
only; the protected credential is deliberately absent).

```bash
systemctl --user status efah-owner-surface
curl -s http://100.93.66.35:8088/owner/health
```

---

## Environment (already prepared — do not rebuild)

```bash
PY=/home/yoav/efah/.venv/bin/python
cd /home/yoav/efah/efah-harness
$PY -m pytest tests/ -q                     # ~2.5 min
$PY tools/check_assertion_hashes.py
$PY tools/gate_d1_07.py ; $PY tools/gate_d1_10.py
PYTHONPATH=src $PY -m evaluation.gate_runner
PYTHONPATH=src $PY -m cli.main project run ./project-pack --mode autonomous
```

TerminusDB `:6363` (db `efah` exists) · protected identity store `:6364`
(main credential correctly gets **401**) · LiteLLM eval + production · Phoenix
`:6007` / OTel `:4319` (**never** 6006/4317/4318 — another project owns those) ·
Plane workspace `efah`.

GitHub App 4460605 mints tokens via `/home/yoav/efah/.work/ghapp.py` →
`.ghtoken` (1-hour expiry, re-run when it 404s). Scope: `efah-harness` only, no
`administration`.

---

## OPEN — what the next session must pick up

### 1. DEC-006 / option B — the sealed verifier (IN PROGRESS, not built)

The owner answered Q1 with **B**. `docs/decisions/DEC-006-*.md` is written and
records the design. **The implementation does not exist yet.**

The owner also made an important correction: the missing piece was never the
*author*. `repositories.yaml` already sets
`bootstrap_mode: role_separated_generation_authorized`, and `model-policy.yaml`
already maps `sealed_holdout_author` and `mutant_author` to models in different
families from `implementer`, on the eval gateway. What is missing is the
**identity boundary and the store**.

**The constraint that decides whether this works:** generation must run *inside*
the verifier identity's process. If holdout text is returned into the builder
process — even transiently — the builder has read it and separation is broken
regardless of which vendor authored it. The builder invokes an opaque subprocess
and receives an exit status and a count, never content.

**Unresolved honesty problem, state it plainly rather than papering over it:**
the builder runs as `yoav`, which holds **passwordless sudo** and is in the
**docker** group. A separate OS user with a `0700` store therefore prevents
*accidental* exposure and makes any access auditable, but is **not** sound
against a deliberate builder. Option A remains the durable path. Do not claim
otherwise in the evidence package; put it in honest debt.

### 2. FINDING-005 — assurance models come from resold subscription pools ⚠️ BIGGEST OPEN ITEM

`docs/decisions/FINDING-005-*.md`. **Measured from the owner's own ckff account
log, not inferred.** `claude-opus-4-8` (sealed holdout author), `claude-opus-4-7`
and `claude-sonnet-5` are all served by **channel 234, group `kiro-pro`** — AWS
Kiro coding-agent accounts. `gemini-3.5-flash` (mutant author) comes from
`gemini-cli` quota.

One channel serves several "different" models, so the family separation the
harness enforces by alias may be fictional at the transport. Official (`官转`)
channels do exist on the platform; the assurance roles are not on them.

**Resolve this before building the sealed verifier.** Holdouts generated through
an unverifiable transport are worth less than the effort of generating them.
Owner options A–D are in the finding; recommendation is A (official credentials
for the nine gate-bearing roles) plus a private mutant corpus regardless.

The ckff account token is at `~/.efah/ckff.env` (mode 0600) if further probing is
needed. `GET https://ckff.dev/api/log/self?p=0&page_size=100&type=0` with
`Authorization: Bearer $CKFF_TOKEN` returns the per-request channel and group.

### 3. FINDING-003 — assurance tier labels contradict their models

`docs/decisions/FINDING-003-*.md`. The owner spotted that `gemini-3.5-flash`
(mutant_author), `glm-5-turbo` (release_verifier) and `claude-sonnet-5`
(compliance auditor) are labelled `tier: frontier` and are not.

Matters because the mutation gate reports **25/25 killed** — a kill rate of 1.0
is only meaningful if the mutants are hard, and easy mutants fully killed
manufacture confidence. `claude-opus-5-thinking` and `gemini-3.1-pro-preview`
are live on the eval gateway and assigned to no role.

**Nothing was changed** — the alias map is owner data. Awaiting owner
adjudication; route it through the control surface rather than opening a second
question round (`autonomy-policy.yaml` forbids drip questions).

### 4. Remaining contract work

- §27 evidence package (GATE-D3-26) and honest-debt ledger.
- 18 gates are `NOT_YET_EXECUTABLE` — mostly Day 2/3 subjects.
- GATE-D3-25 needs a real green PR merged by CI, blocked behind `hidden_holdout`.
- **Required status checks are configured but `strict` is off**, so GitHub does
  not enforce "branch up to date". `auto_merge_requirements.branch_up_to_date`
  must therefore be enforced by the harness, not assumed.

---

## Standing corrections earned the hard way

- **Naming a thing is not depending on it.** Three separate lanes hardcoded the
  sealed repository names *in order to deny them*, which GATE-D1-08 A2 forbids.
  Denylists now derive from `repositories.yaml → sealed_repos` at runtime via
  `governance/protected.py`. The same bug appeared in GATE-D1-07 A4 (a CI step
  *named* "Anthropic credentials removed" was flagged as depending on Anthropic).
  If a check fires on a guardrail, fix the check — never relax the gate.
- **Do not fake a station to make a board green.** Station 11 reports
  `UNAVAILABLE`. §14.4's pass condition is that services are *exercised with
  evidence*.
- **`RUNNING` exits non-zero.** Exiting 0 mid-run is the "mostly done" report
  §6.2 forbids. `VERIFIED_COMPLETE` is the only zero exit.
- **The composition registry caught its own author** — twelve modules declared
  and unreachable. Trust it over your own diagram.
- **Global throttle is account-wide: 90 req/min**, shared by every process via a
  file lock. An unthrottled fan-out self-inflicts 429s that are indistinguishable
  from genuine model failure — i.e. fabricated evidence.

---

## Kickoff for the next session

```
Read HANDOFF-002.md at the repo root, then re-ground from ../project-pack
(contract v1.1 = v1.0 + AMENDMENT-001) and from git log. Do not rely on any
conversation history.

Continue the EFAH build under contract EFAH-CONTRACT-001 v1.1. DEC-001, DEC-002,
DEC-005 and DEC-006 are signed and bind you. delivery_priority is binding and may
not be reordered.

FIRST read FINDING-005: the assurance models are served from resold subscription
pools (measured, not inferred), and that decision gates whether sealed-holdout
work is worth doing. Do not start generating holdouts before it is resolved.

Then: implement DEC-006 option B — the verifier service identity and sealed
store, with generation running inside that identity, never in the builder
process. Then the Section 27 evidence package.

Continue autonomously through repair, verification, PR and merge whenever the
auto-merge gates pass. Stop only at VERIFIED_COMPLETE, a permitted typed blocker,
FAILED_CONTRACT, FAILED_ASSURANCE, or CANCELED.
```
