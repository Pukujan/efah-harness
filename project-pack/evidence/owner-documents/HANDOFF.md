# EFAH Handoff — Claude Code (builder) + Cowork (owner seat)

Contract: EFAH-CONTRACT-001 v1.0 · Deadline: 2026-08-03 · Vendor-neutral after deadline: yes
Governing files: `EFAH_FINAL_BUILD_CONTRACT_v1_0.md` (authoritative prose) + `.yaml` (machine controls). The contract wins over this handoff.

---

## 1. Tool split (who does what)

| Surface | Role | Why |
|---|---|---|
| **Claude Code** | Temporary builder. Executes the contract end to end: preflight → walking skeleton → gates → PRs → auto-merge → evidence package. | The contract names `temporary_builder: claude_code`. The work is FastAPI/LangGraph/TerminusDB services, CI, worktrees, hooks — Code's home turf. |
| **Claude Cowork** | Owner seat. Prepares/edits the project pack, watches Plane, answers the ONE batched blocker round, reviews the final evidence package, drafts any contract amendment. | Cowork must NOT build. Plane is a projection, not truth (`project_management_projection: plane`); Cowork is your window, not a second builder. |
| **Human (you)** | Owner. Only interrupted for the typed list in `autonomy.human_interrupts_only`. | Everything else continues autonomously per `autonomy.continue_without_human_confirmation: true`. |

**Usage windows (as of 2026-08-01):** Cowork 5-hour limits are 2x through Aug 5; Claude Code weekly limits are +50% through Aug 19. Both cover the Aug 1–3 build. Verify current terms at support.claude.com before launch.

---

## 2. Repo topology (non-negotiable per contract)

```
BUILD SIDE (builder has access)          SEALED SIDE (builder NEVER has access)
─────────────────────────────           ──────────────────────────────────────
harness repo (new, per Section 5)       Eval-lab-verifier  (private repo,
  └─ eval lab module lives here           separate service identity, sealed
Eval-lab (existing)                       release holdouts, hard gold)
  └─ prior art: gates, dossier,
     ADRs, threat model — port these
stupidly-simple-cortex
  └─ reference only unless declared
     in repositories.yaml
```

Contract clauses that force this: `hidden_verification: separate_repository_and_service_identity`; `role_separation: implementer_no_sealed_holdout_access`, `builder_ne_holdout_author`, `builder_ne_final_adjudicator`; acceptance checks `protected_verifier_isolation` and `visible_hidden_mutant_same_candidate_commit`.

Rules:
1. The builder session gets credentials for the BUILD side only. No verifier repo token, no verifier DB creds, ever. An acceptance check will actively try to prove isolation — don't "help" by wiring shared access.
2. The verifier runs as its own service identity and pulls candidate commits; the builder submits `CANDIDATE_COMPLETE` and receives verdicts, never holdout content.
3. Verifier repo staying 404 to the public/builder is correct behavior. Do not open it.
4. Eval-lab's Temporal-based skeleton conflicts with `non_goals: temporal_initial_runtime`. Resolution (owner decision, settle BEFORE launch so it doesn't consume the question round): the contract supersedes — LangGraph is the permanent runtime with `AsyncSqliteSaver` behind an adapter (non-authoritative). Eval-lab's Temporal skeleton is reusable evidence and pattern source, not the base. If you disagree, amend the contract first (Section 1.3 change rule); don't let the builder reinterpret.

---

## 3. Owner pre-launch checklist (only you can do these)

Do these in Cowork or by hand BEFORE starting Claude Code. Every unchecked box below becomes a typed blocker or a burned question in the single allowed round (`max_initial_owner_question_rounds: 1`).

- [ ] **Decide the build repo** and write it into `repositories.yaml` (recommendation: fresh `efah-harness` repo structured per contract Section 5; Eval-lab listed as reusable-source).
- [ ] **Grant repo access**: builder token for build-side repos only. Confirm the builder token CANNOT reach `Eval-lab-verifier`.
- [ ] **GitHub setup for autonomy**: branch protection on `main` with required checks; PRs opened via a GitHub App or PAT (the default `GITHUB_TOKEN` won't trigger CI on its own PRs, which silently breaks `auto_repair_ci` and `auto_merge`); enable auto-merge on the repo.
- [ ] **LiteLLM proxy** running with your existing config; note the base URL + key ref (`model_gateway: existing_litellm_proxy`, `uses_existing_litellm_config: true`).
- [ ] **Context7**: both credentials available (`credentials: 2`), snapshot caching allowed.
- [ ] **TerminusDB** reachable (Docker is fine) — it is the authoritative graph; Plane is projection only.
- [ ] **Plane** workspace + API key created; project slug noted for `plane.yaml`.
- [ ] **Secrets**: fill `secrets.refs.yaml` with references (names/paths), never values.
- [ ] **Verifier bootstrap**: seed `Eval-lab-verifier` (separate identity) with initial sealed holdouts + oracle definitions, or explicitly authorize the contract's role-separated path for generating them. The builder cannot author its own holdouts.
- [ ] **Post-deadline check**: nothing essential may depend on Claude access after 2026-08-03 (`vendor_neutral_after_deadline: true`). LangGraph + LiteLLM + CI must run the show without Claude Code.

---

## 4. Project pack — required files and starter stubs

Contract requires: `contract.md`, `contract.yaml`, `project.yaml`, `repositories.yaml`, `environments.yaml`, `model-policy.yaml`, `methodology-policy.yaml`, `dependency-policy.yaml`, `autonomy-policy.yaml`, `plane.yaml`, `secrets.refs.yaml`. Optional dirs: `acceptance/visible`, `acceptance/oracle-definitions`, `evidence/owner-documents`, `evidence/context7-snapshots`.

Launch command the harness itself must implement and you will eventually run: `harness project run ./project-pack --mode autonomous`.

Minimal stubs (Claude Code may materialize/expand these in preflight; only TODO fields are owner-material — "no silent defaults for material fields"):

```yaml
# project.yaml
project: {id: EFAH-001, name: EFAH Harness and Eval Lab, contract_id: EFAH-CONTRACT-001, contract_version: "1.0", target_deadline: 2026-08-03, contract_review_interval_phases: 3}

# repositories.yaml
build_repos:
  - {name: efah-harness, url: TODO_owner, role: primary_build, default_branch: main}
  - {name: Eval-lab, url: https://github.com/Pukujan/Eval-lab, role: reusable_source}
sealed_repos:
  - {name: Eval-lab-verifier, role: protected_verifier, builder_access: forbidden, service_identity: separate}

# environments.yaml
environments:
  dev: {terminusdb_url: TODO_owner, litellm_base_url: TODO_owner, plane_base_url: TODO_owner, phoenix_url: TODO_owner}

# model-policy.yaml
router: {deterministic: true, blinded_aliases: true, source: existing_litellm_config, availability_probe_required: true}
aliases: TODO_owner_map_roles_to_litellm_aliases   # builder/critic/adjudicator/judge per role_separation

# methodology-policy.yaml
default: {walking_skeleton_first: true, dependency_first: true, oracle_hierarchy: contract, deterministic_oracle_preferred: true}

# dependency-policy.yaml
policy: {integrate_by_default: true, reimplementation_requires: recorded_evidence_backed_blocker, context7_snapshot_required: true, pin_versions: true}

# autonomy-policy.yaml
autonomy: {continue_without_human_confirmation: true, auto_open_pr: true, auto_repair_ci: true, auto_merge: true}
human_interrupts_only: [OWNER_SCOPE_DECISION, OWNER_PRIORITY_DECISION, OWNER_RISK_ACCEPTANCE, MISSING_REQUIRED_CREDENTIAL, IRREVERSIBLE_EXTERNAL_ACTION, CONTRACT_AMENDMENT_REQUIRED, IRRESOLVABLE_EVIDENCE_CONFLICT]

# plane.yaml
plane: {workspace: TODO_owner, project: TODO_owner, api_key_ref: secrets.plane_api_key, mode: projection_only}

# secrets.refs.yaml
refs: {litellm_key: TODO_ref, plane_api_key: TODO_ref, terminusdb_auth: TODO_ref, github_app_or_pat: TODO_ref, context7_key_1: TODO_ref, context7_key_2: TODO_ref}
```

---

## 5. Kickoff prompt — paste into Claude Code (repo root containing the contract + project-pack/)

```
Read START_HERE_FOR_CLAUDE_CODE.md, then the full EFAH_FINAL_BUILD_CONTRACT_v1_0.md, and validate EFAH_FINAL_BUILD_CONTRACT_v1_0.yaml. The contract governs; do not reinterpret or broaden the selected architecture.

Then inspect ./project-pack, the declared repositories, the LiteLLM config, Context7 access, CI, and existing artifacts BEFORE asking anything.

Produce the required preflight record first: discovered repo/branch state; existing components to reuse (including which Eval-lab gates, dossier, ADR and threat-model patterns you will port — note Temporal is a contract non-goal, LangGraph is the permanent runtime); missing genuine inputs; walking-skeleton implementation order; initial task/dependency graph; Day-1 executable acceptance gates; and confirmation that no essential delivered component depends on Claude access after 2026-08-03.

You get at most ONE batched question round, only for typed blockers the contract permits (Section 29). Bind every answer to contract version 1.0. Then execute: modular-monolith control plane, complete walking skeleton, protected-verifier path as an early milestone, fresh bounded worker sessions with durable state outside model context. Continue autonomously through repair, verification, PR, and merge whenever the auto-merge gates pass. Stop only at VERIFIED_COMPLETE, a permitted typed blocker, FAILED_CONTRACT, FAILED_ASSURANCE, or CANCELED.
```

Session hygiene: one task/phase per session; keep durable state in TerminusDB/git, not chat context; new session after each milestone rather than compacting a degraded one.

---

## 6. Cowork prompt — your oversight seat (separate session, owner credentials)

```
You are the OWNER's assistant for EFAH-CONTRACT-001 v1.0, not a builder. Do not write harness code.
1) Help me finish the project pack: validate the stubs, flag every TODO_owner field, and check nothing grants the builder access to Eval-lab-verifier.
2) When the builder posts its single batched blocker round, help me draft typed answers and record each as a Decision bound to contract v1.0.
3) Monitor the Plane projection and the repo: summarize task states, scope-drift findings, gate results, and anything sitting in a BLOCKED_* or FAILED_* state.
4) At the end, walk me through the VERIFIED_COMPLETE evidence package against the contract's acceptance_checks list and flag any check without evidence.
Never approve scope changes yourself — route them through the contract's change rule as a ContractVersion amendment.
```

---

## 7. What to expect during the run

- **Legit interrupts** (answer these): the seven `human_interrupts_only` types above. Anything else asking for permission = the builder drifting; point it back to the contract.
- **Auto-merge only fires when ALL pass**: contract unchanged/approved · zero unresolved scope drift · visible + integration + composition tests · hidden holdout · mutation gate · oracle health · provenance · dependency policy · zero unresolved high-risk findings · no protected-asset access · branch up to date. If merges stall, check the GitHub App/PAT + branch protection first — that's the usual silent killer.
- **Three-day shape** (contract `three_day_plan`): Day 1 structure/schemas/skeleton+CI · Day 2 workers, code-intel, RAG, verifier lane, observability, drift/review, auto-PR · Day 3 end-to-end rep project, rejection drills (wiring omission, stale worker, drift, mutant), checkpoint resume, green auto-merge, evidence package.
- **Success** = `VERIFIED_COMPLETE` with the evidence package — not "mostly done."
