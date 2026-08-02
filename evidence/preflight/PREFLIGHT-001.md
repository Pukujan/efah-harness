# PREFLIGHT-001 — Evidence-First Engineering Harness

**Contract:** EFAH-CONTRACT-001 **v1.1** (v1.0 as amended by AMENDMENT-001, owner-approved 2026-08-02)
**Project:** EFAH-001 · **Produced:** 2026-08-02 · **Deadline:** 2026-08-03
**Produced by:** temporary builder (Claude Code) · **Authority level:** 4 (measured live state)

All state below is **freshly observed**, not quoted from the pack. Where live
state contradicts a pack assertion, the contradiction is stated explicitly
(contract §8.1 "repository and state preflight": *stale or contradictory state is
reconciled before planning*).

---

## 1. Governing version and §1.3 amendment status

Contract v1.1 = v1.0 + AMENDMENT-001 (§11.7 owner control surface). Owner
approval is recorded; steps 1–4 of §1.3 are complete in the pack. The remaining
three steps are the builder's, at intake:

| §1.3 step | Status | Where |
|---|---|---|
| 5. attributable TerminusDB commit | scheduled — WS-A/T-004 | `efah` db, branch `intake/contract-v1.1` |
| 6. recompiled workflow and gate definitions | scheduled — WS-A/T-005 | compiler output + GATE-D1-10 wired into the D1 runner |
| 7. revalidation of affected objects | scheduled — WS-A/T-006 | §11.3 router, §11.6 views, §14.4 skeleton trace, §10.7 interrupts |

Amendment impact is **additive**: one new walking-skeleton step (`owner control
surface` after `dashboard update`), one new blocking Day-1 gate (GATE-D1-10), no
new interrupt types, no change to the §11.3 router rule. Nothing in v1.0 is
weakened, so revalidation is confined to those four objects.

**DEC-001** (LangGraph supersedes Temporal) and **DEC-002** (gate-bearing traffic
routes through the eval gateway) are signed and binding. Neither is reopened.
Eval-lab's Temporal skeleton is pattern source only; Temporal will not appear as
a dependency, a runtime, or a proposed checkpoint backend.

---

## 2. Discovered repository and branch state

| Repo | Declared | **Measured** | Consequence |
|---|---|---|---|
| `Pukujan/efah-harness` | private, `main` exists (README) | **EXISTS but has ZERO refs** — `git ls-remote` returns nothing | `main` does **not** exist. Branch protection cannot be applied to a branch that does not exist. Builder creates `main` with this commit. |
| `Pukujan/Eval-lab` | reusable source, read | **reachable, public**; `HEAD=a4001ef`, many `agent/*` branches | Reuse path is open. Default branch resolved by probe (was `TODO_builder_probe`). |
| `stupidly-simple-cortex` | `declared: false`, out of scope | not probed | Honoured. Out of scope. |
| `Pukujan/efah-lab-verifier` (sealed) | forbidden, `not_supplied_to_builder` | **no refs returned under either identity** | Correct isolation — *and* see Blocker Q1: it holds no reachable holdouts. |
| `Pukujan/Eval-lab-verifier` (sealed, declared unrelated) | forbidden | **anonymously world-readable over HTTPS**, exposes `refs/heads/private/source-holdouts` | Finding F-01 below. **Not read, not cloned, not fetched.** |

**Local:** `/home/yoav/efah/efah-harness`, branch `main`, **no commits yet**,
remote `git@github-efah:Pukujan/efah-harness.git`.

### Identity and access, measured

- **SSH deploy key** (`~/.ssh/efah_deploy`) authenticates as *the repository*
  `Pukujan/efah-harness` — repo-scoped, push/pull only, **no API surface**.
- **GitHub App 4460605** installed on `Pukujan/efah-harness` **only**.
  Permissions measured live: `contents:write, issues:write, pull_requests:write,
  actions:write, checks:write, metadata:read`.
- **`administration` is absent** → the builder **cannot** configure branch
  protection or required status checks. That is an owner action. Per
  `repositories.yaml` lines 30–33 the builder verifies and raises
  `BLOCKED_EXTERNAL_ACCESS`; it does **not** spend a question slot asking whether
  it was done. Recorded as **BEA-01**.
- **Credential-reference discrepancy, resolved by inspection (not asked).**
  `secrets.refs.yaml` declares `github_app_or_pat: env:GITHUB_APP_PRIVATE_KEY`.
  That variable is **unset**. The environment instead provides
  `GITHUB_APP_PRIVATE_KEY_PATH` → a 1675-byte PEM that mints installation tokens
  successfully. Contract §7.1 forbids asking the owner about existing
  configuration; §7.2 classes this a repository fact. **Resolved by derivation**
  and recorded as DEC-003 (low-consequence, reversible, disclosed). The adapter
  accepts either ref form. **`MISSING_REQUIRED_CREDENTIAL` does not fire.**
- `gh` CLI is **not installed**. All GitHub operations go through the App via
  the REST API — no dependency on a missing binary.

---

## 3. Live service state (all probed 2026-08-02, this session)

| Service | Endpoint | Result |
|---|---|---|
| TerminusDB — authoritative | `localhost:6363` | **200**; container `terminusdb` up; **`/api/db` returns `[]`** — the `efah` database does not exist yet. Builder creates it. |
| TerminusDB — protected identity | `localhost:6364` | **200**; separate container, separate volume, bound `127.0.0.1`; **`admin/efah_protected` exists** |
| **Protected-store isolation** | main admin cred → `:6364` | **HTTP 401 — PASS.** Contract §11.2 satisfied by the *isolated instance* option. Evidence captured for GATE-D1-08. |
| LiteLLM **eval** (gate-bearing) | `litellm-eval-production…` | **200**, 0.12 s |
| LiteLLM **production** (candidate) | `litellm-production-8656…` | **200**, 0.15 s |
| **Gateway key isolation** | cross-key probe | confirmed in DEC-002 §"Verified 2026-08-02"; re-probe scheduled as a gate, not assumed |
| Context7 | `context7.com` | **200** (primary credential) |
| Plane | `app.plane.so` workspace `efah` | **200** |
| Phoenix (EFAH-dedicated) | `localhost:6007` | **200**, container `efah-phoenix` |
| OTel collector (EFAH) | `localhost:4319` | listening (gRPC; HTTP GET correctly yields no response) |
| Port-collision avoidance | — | **verified**: `cortex-phoenix` holds `:6006` and `cortex-otel-collector` holds `:4317/4318`; EFAH is on `:6007/:4319` and does not share them, as `environments.yaml` requires |

**Tailnet address for the owner control surface:** `100.93.66.35` (GATE-D1-10 A9
requires mobile-viewport reachability over the private network).

**Environment prep is accepted as done and not redone:** `validate_pack.py
project-pack --self-test` → **0 errors, 0 warnings, 6 TODO fields, 7/7 planted
breaches caught**; `hash_assertions.py --check` → **27 gates, 0 violations**. The
assertion baseline is intact, so no gate has been weakened.

---

## 4. Components to reuse (dependency-first, §14.2)

Integrate-by-default. No custom equivalent of any selected dependency will be
authored; each external system sits behind an adapter.

**Ported from `Eval-lab` as pattern source** (prior art, cited — not vendored
wholesale, and explicitly *not* its Temporal runtime, per DEC-001):

| Pattern | Ported into | Note |
|---|---|---|
| executable gate definitions | `src/evaluation/` gate runner | pack's 27 YAML gates are the authority; Eval-lab supplies the *runner* shape |
| evidence dossier format | `src/evidence/` + §27 package builder | |
| architecture decision records | `docs/decisions/` | ADR template and numbering |
| threat model | `docs/architecture/security-and-trust-boundary.md` | **frozen** — §19.5 makes an unlinked security finding `OUT_OF_SCOPE_OBSERVATION`, not new work |

**Mature dependencies (integrated, versions pinned at install and recorded with
lockfile source + Context7 snapshot per §16.3):** LangGraph + `AsyncSqliteSaver`
behind a checkpoint adapter · LiteLLM (existing config, both gateways, **not**
redesigned) · TerminusDB · Plane (projection only) · FastAPI + Pydantic ·
Docling · LanceDB · LlamaIndex (ingestion/retrieval components only) · Inspect
AI · Promptfoo · OpenTelemetry + Phoenix · git/ripgrep/Tree-sitter.

**Explicitly not built:** workflow engine, graph database, provider router,
vector index, eval runner, PM UI (`dependency-policy.yaml → prohibited`).

---

## 5. Missing genuine inputs

Exactly **one** item requires the owner. Everything else was resolved by
inspection, probe, or recorded decision, per the §7.1 resolver order.

**Q1 — sealed holdouts have no path into existence.** `IRRESOLVABLE_EVIDENCE_CONFLICT`
+ `MISSING_REQUIRED_CREDENTIAL`. `hidden_holdout` is 1 of the 13
`auto_merge_requirements`, so while it cannot return PASS, auto-merge can never
fire and `VERIFIED_COMPLETE` is unreachable regardless of code quality — the
failure OWNER_TODO Tier 0 §2 predicts would surface on Day 3. Surfaced now, at
intake. Details and options are in the single batched GitHub issue.

**Resolved without asking:**

| Item | Resolution | Class |
|---|---|---|
| GitHub App key ref mismatch | derived from environment (DEC-003) | repository fact |
| Eval-lab default branch | probed | repository fact |
| Branch protection / required checks / auto-merge | **BEA-01**, owner action, not a question | live empirical fact |
| Representative project | builder selects and records (`project.yaml` authorizes it) — see §6 | recorded decision |
| Deployment target | **waived** by owner 2026-08-01 → honest debt in §27 package, *not* reported as passed | recorded decision |
| Temporal vs LangGraph | DEC-001, signed | recorded decision |
| Gateway split | DEC-002, signed | recorded decision |
| Judge calibration threshold | deliberately unset → all judges advisory; deterministic oracles carry the gates | recorded decision |
| Dependency versions | probed at install and pinned | repository fact |

### Findings recorded, not asked

- **F-01 — `Eval-lab-verifier` is anonymously world-readable**, exposing
  `refs/heads/private/source-holdouts`. `repositories.yaml` declares it
  `builder_access: forbidden`. The builder's deploy key correctly receives
  *Repository not found*, but the **host has unauthenticated network reachability
  to it**. The builder has **not** read, cloned, fetched, or listed its contents
  beyond the ref names returned by an isolation probe, and will not. Reported for
  owner action. GATE-D1-08 A1 is satisfied *for the builder identity*; this is a
  separate exposure of the repo itself.
- **F-02 — `repositories.yaml` line 19–21 is contradicted by live state.** It
  asserts `efah-harness` was created with a README so `main` exists; it has zero
  refs. Reconciled by the builder creating `main`. Non-blocking, recorded because
  §8.1 requires contradictions to be reconciled rather than absorbed.

---

## 6. Representative project (recorded decision, DEC-004)

`project.yaml` sets `selection_mode: builder_selects_and_records`. Selected:
**the harness's own contract-compilation path, carried end to end as work unit
WU-REP-001** — compile `contract.md` v1.1 → requirements → tasks → dependency
graph → a code change → visible test → protected verdict → CI gate → merge.

Against the four recorded constraints: **real, not synthetic** (it is this
build's own critical path, not a fixture); **deterministic oracle** — ORACLE-001
composition-reachability and ORACLE-003 provenance-binding both decide it without
a model in the verdict path; **completable in the window**; **exercises the full
path, not a subset** — every §14.4 station including the amendment's new one.

---

## 7. Walking-skeleton order (§14.4 + AMENDMENT-001)

Built in this order, each station real before the next begins. **Zero
placeholders** — a station that cannot be exercised end to end fails the phase
rather than being stubbed.

```
 1 project-pack import        → validate, hash, import to ISOLATED TerminusDB branch
 2 TerminusDB commit          → attributable immutable Contract/Project entities (v1.1)
 3 LangGraph project run      → project_graph on the checkpoint adapter
 4 task creation + Plane      → ledger events → one-way projection
 5 model alias routing        → LiteLLM, gateway split enforced (DEC-002)
 6 fresh worker session       → bounded, no persistent chat memory
 7 tool / repository action   → real edit under a lease in an owned worktree
 8 artifact submission        → content-addressed, hashed, registered
 9 trace + provenance         → OTel span → Phoenix, correlated per §23
10 visible test               → assertion-hashed, executed against the candidate commit
11 protected verifier call    → verifier-interface, verdict shape only
12 oracle result              → deterministic, health emitted, no judge in path
13 CI gate                    → GitHub Actions on the real commit
14 dashboard update           → read projection
15 OWNER CONTROL SURFACE      → §11.7 — LangGraph-backed, mobile, Anthropic-free  ← AMENDMENT-001
```

Priority order is `project.yaml → delivery_priority` and is **not reordered**:
(1) complete skeleton, (2) GATE-D1-07 vendor neutrality with Anthropic
credentials removed, (3) GATE-D1-10 owner control surface, (4) everything else.

---

## 8. Task / dependency graph (initial)

```
WS-A  Contract & compilation           WS-B  Graph authority & provenance
  T-001 pack loader + schema bind        T-010 TerminusDB adapter + branch/commit
  T-002 contract compiler                T-011 control-plane schema (39 entities)
  T-003 requirement/task/dep graph       T-012 protected identity store (:6364)
  T-004 §1.3 attributable commit  ◄──────┘     (isolated instance, 401 proven)
  T-005 §1.3 recompiled gates            T-013 provenance + content hashing
  T-006 §1.3 revalidation

WS-C  Runtime (LangGraph)              WS-D  Models & workers
  T-020 checkpoint adapter               T-030 model router (deterministic)
  T-021 project/intake/task graphs       T-031 gateway split enforcement (DEC-002)
  T-022 lease + generation fencing       T-032 alias blinding + protected map
  T-023 resume-without-restart           T-033 fresh bounded worker sessions
                                         T-034 global throttle 90 rpm account-wide

WS-E  Assurance                        WS-F  Surfaces
  T-040 gate runner (27 gates)           T-050 FastAPI app + middleware
  T-041 verifier-interface (submit only) T-051 controllers + read projections
  T-042 oracle runner + health           T-052 Plane projection adapter
  T-043 visible/mutant suites            T-053 OWNER CONTROL SURFACE (§11.7)
  T-044 drift engine                     T-054 dashboard views

WS-G  Composition & release            WS-H  Evidence
  T-060 composition root + registration  T-070 dossier + §27 package builder
  T-061 architecture tests               T-071 Context7 snapshot cache + hashing
  T-062 CI workflows + gates             T-072 dependency registry
  T-063 PR + auto-merge via App          T-073 honest debt ledger

Critical path:  T-001 → T-002 → T-010 → T-011 → T-004 → T-020 → T-021 → T-060
                → T-050 → T-040 → T-062 → T-053
Hard blocker:   T-041 hidden_holdout ← Q1 (owner). Everything else proceeds.
```

Parallel lanes run in isolated git worktrees under leases; WS-B, WS-C, WS-D,
WS-F, WS-H have no mutual dependency after T-001/T-002 land.

---

## 9. Day-1 executable gates

Executable against the walking skeleton the moment each station lands. All ten
are `oracle_type` deterministic or static — **no model judge in any verdict
path**, consistent with `judge_calibration.posture: all_judges_advisory`.

| Gate | Check | Day-1 executable |
|---|---|---|
| GATE-D1-01 | pack imports to an isolated TerminusDB branch | yes |
| GATE-D1-02 | schemas validate and are version-bound | yes |
| GATE-D1-03 | contract compiles to project/task/dependency graph | yes |
| GATE-D1-04 | LangGraph resumes from checkpoint without redoing work | yes |
| GATE-D1-05 | fresh worker sessions execute bounded tasks via LiteLLM | yes |
| GATE-D1-06 | blinded model identity | yes |
| **GATE-D1-07** | **vendor neutrality, Anthropic credentials removed** | **yes — priority 2** |
| GATE-D1-08 | protected verifier isolation | **partially proven already**: main→protected 401 captured; A1/A3 confirmed for the builder identity |
| GATE-D1-09 | mechanical commit/trace/artifact binding | yes |
| **GATE-D1-10** | **owner control surface** (AMENDMENT-001) | **yes — priority 3** |

Day-2/3 gates are declared now so none can be discovered late and deferred.
GATE-D3-25 (auto-merge) additionally depends on **BEA-01** (branch protection —
owner) and GATE-D2-19/D3-24 depend on **Q1** (holdouts — owner).

---

## 10. Vendor-neutrality confirmation (contract §0, `product.vendor_neutral_after_deadline`)

**Confirmed: no essential delivered component depends on Claude access after
2026-08-03.**

- The permanent runtime is **LangGraph**, not the Claude Agent SDK. DEC-001 binds
  it; `claude_agent_sdk` is a prohibited dependency.
- All model access is through the **existing LiteLLM** gateways by **alias**. The
  router is a deterministic policy service; swapping an alias is a config change,
  not a code change.
- Three role aliases currently map to Anthropic models
  (`visible_test_author`, `sealed_holdout_author`, `contract_compliance_auditor`).
  These are **gate-bearing evaluation roles reached through LiteLLM aliases**, not
  SDK imports, and each has a declared cross-family fallback preserving the
  separation rules. **GATE-D1-07 A2/A5 are proven by running the full skeleton
  with every Anthropic credential unset** — already the ambient state on this host
  (`ANTHROPIC_API_KEY` is **unset** right now).
- **Claude Code is an optional worker adapter behind the worker interface**
  (GATE-D1-07 A3/A5). Disabling it leaves a working LiteLLM-backed adapter; the
  adapter-removal probe is part of the gate, not a claim.
- **No CI step requires Claude access** (GATE-D1-07 A4). Workflows call the
  harness CLI and the App identity only.
- **AMENDMENT-001 closes the control half**: after 2026-08-03 the owner can
  observe, answer a typed blocker, resume/retry/cancel a work unit, and issue a
  contract-bounded instruction — from a phone, over the tailnet, with Anthropic
  credentials removed. Without it the harness would be vendor-neutral in
  execution and Claude-dependent in practice.

The one genuine dependency on the builder is **build-time only**: it ends when
the contract does.

---

## 11. Autonomy posture

Proceeding without further confirmation per `autonomy-policy.yaml`
(`continue_without_human_confirmation: true`; `must_not_interrupt_for` includes
ordinary test/integration/CI failure, retry selection, PR creation, and green
auto-merge). The single batched question round is posted as one GitHub issue.
After the 4-hour SLA with no answer, `BLOCKED_OWNER_DECISION` is recorded **on
the blocked tasks only** and every other lane continues. The project is not
halted.

Terminal states remain the only stopping points: `VERIFIED_COMPLETE`, a permitted
typed blocker, `FAILED_CONTRACT`, `FAILED_ASSURANCE`, `CANCELED`.
