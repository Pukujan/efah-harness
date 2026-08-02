# FINAL BUILD CONTRACT

## Evidence-First Cross-Vendor Long-Running Software Engineering Harness and Eval Lab

**Document ID:** EFAH-CONTRACT-001  
**Version:** 1.0  
**Status:** FINAL EXECUTION CANDIDATE  
**Prepared:** 2026-08-01  
**Target deadline:** 2026-08-03, before Claude Code access expires  
**Initial task families:** software engineering and DevOps  
**Primary builder during the deadline window:** Claude Code  
**Permanent runtime:** LangGraph, not Claude SDK  

> **Execution authorization:** When the owner supplies this file to Claude Code with an instruction to execute it, Claude Code shall treat this exact version as the governing build contract. Claude Code may ask one batched blocker round only where the contract and supplied project pack cannot resolve a material decision. Claude Code shall otherwise continue autonomously through implementation, verification, pull request, repair, and merge according to this contract.

---

# 0. Claude Code Start Directive

Claude Code is being asked to **build the vendor-neutral harness described here**, not to become the harness runtime.

Claude Code MUST:

- use mature dependencies and adapters before authoring equivalent custom infrastructure;
- preserve the approved project scope and requirements;
- build the walking skeleton before broad module expansion;
- build the Eval Lab and project control plane as first-class deliverables, not as final cleanup;
- keep all essential runtime paths independent of Claude Code and Anthropic SDKs;
- use LangGraph as the durable agent workflow runtime;
- use TerminusDB as the authoritative project, contract, ontology, dependency, provenance, and assurance graph;
- use Plane as the human-readable project-management projection;
- use the existing LiteLLM configuration and project documentation rather than redesigning provider access;
- use Context7 for current, version-specific dependency documentation, caching, and version-diff inputs;
- create fresh bounded worker sessions for long-running work rather than relying on one growing conversation;
- run separate implementer, test-author, holdout-author, mutant-author, critic, judge, and auditor roles as required;
- record all work in the task ledger and artifact/evaluation registries;
- continue automatically after tests pass, including PR creation, CI repair, and auto-merge when all required gates are green;
- return a final evidence package or a typed terminal blocker, not an ambiguous “mostly done” report.

Claude Code MUST NOT:

- replace LangGraph with Claude Agent SDK, Temporal, or a model-driven orchestrator;
- create a free-form project-manager agent with authority to change scope;
- make itself the source of truth, final judge, hidden-test author, or release authority;
- rebuild a mature dependency without a recorded, evidence-backed `BUILD_VS_INTEGRATE` decision;
- expand cybersecurity scope beyond the approved threat model;
- stop after opening a PR when auto-merge conditions are satisfied;
- claim a module is complete when it is not wired into a real end-to-end path;
- restart the repository merely because one worker, graph node, or provider fails;
- silently defer required work to “phase 2” when the work is part of this contract.

---

# 1. Contract Authority and Interpretation

## 1.1 Normative language

- **MUST / MUST NOT**: required for acceptance.
- **SHOULD / SHOULD NOT**: expected unless an explicit evidence-backed exception is recorded.
- **MAY**: optional and must not delay required delivery.

## 1.2 Authority order

| Priority | Authority |
|---|---|
| 1 | This owner-approved contract version and approved amendments. |
| 2 | Machine-compiled requirements, policies, schemas, and gates generated from this contract. |
| 3 | Owner-recorded decisions linked to this contract. |
| 4 | Current project artifacts, code, tests, probes, and measured live state. |
| 5 | Version-pinned official documentation and primary evidence. |
| 6 | Approved methodologies and operating procedures. |
| 7 | Model output, recommendations, and inferred conventions. |

No agent, judge, reviewer, dependency, dashboard, or implementation artifact may override a higher authority.

## 1.3 Change rule

No material contract change is valid unless it has:

1. an exact proposed clause or structured diff;
2. affected requirements and impact analysis;
3. owner approval;
4. a new contract version;
5. an attributable TerminusDB commit;
6. recompiled workflow and gate definitions;
7. revalidation of affected tasks, artifacts, tests, oracles, gold cases, and release candidates.

---

# 2. Product Definition

The product is a **contract-compiled, evidence-first, vendor-neutral, long-running multi-agent software engineering and assurance platform**.

It shall:

1. accept a complete project pack and repository;
2. research and resolve only the missing facts needed to execute;
3. ask the owner only genuine material blocker questions;
4. compile the approved contract into tasks, dependencies, roles, policies, success conditions, failure conditions, and gates;
5. execute work through fresh cross-vendor worker sessions;
6. preserve durable project state outside all model contexts;
7. prevent scope drift and circular validation;
8. mechanically verify repository changes, traces, tool calls, test execution, artifacts, and release evidence;
9. independently evaluate work through visible tests, sealed holdouts, mutants, gold cases, oracles, and calibrated judges;
10. continue until verified completion, a typed owner blocker, or a typed assurance failure;
11. promote only objectively verified results into trusted RAG, KEDB, and hard-gold stores.

## 2.1 The system is not

- a Claude replacement tied to one project;
- a Claude SDK application;
- a free-form LLM orchestrator;
- a model voting council;
- an agent chat transcript used as project memory;
- a generic RAG chatbot;
- a collection of disconnected scripts;
- a project-management dashboard without enforceable workflow;
- a system where the builder writes and passes only its own visible tests;
- a promise of a fully scaled universal enterprise platform within the deadline.

## 2.2 Required delivery standard

The deadline deliverable is a **properly wired, production-shaped reference application** using mature components. It must carry one representative software-engineering or DevOps project through the complete real path, not simulate the path with placeholders.

---

# 3. Architecture Classification

The system shall be implemented as:

> **A domain-oriented modular-monolith control plane with distributed worker, model, retrieval, evaluation, CI/CD, and observability integrations.**

## 3.1 Modular-monolith control plane

The core application shall be one understandable repository and primary deployment unit with explicit domain modules, typed interfaces, enforceable dependency rules, and one composition root.

The modular monolith shall own:

- project and portfolio state;
- contract and requirement management;
- methodology selection;
- task and dependency ledgers;
- assignment leases and fencing;
- scope-drift detection;
- contract compilation;
- LangGraph workflow definitions;
- model-routing policy;
- artifact, evaluation, oracle, holdout, mutant, and gold registries;
- knowledge promotion;
- impact and invalidation analysis;
- API, controllers, middleware, and dashboard projections.

## 3.2 Distributed integrations

The runtime may communicate with separately deployed services:

- TerminusDB;
- Plane;
- LiteLLM;
- Context7;
- LanceDB or the approved retrieval index;
- object storage;
- Inspect AI;
- Promptfoo;
- Phoenix and OpenTelemetry;
- GitHub and CI runners;
- sandboxed coding workers;
- protected verifier repository/service;
- deployment environments.

## 3.3 Required architecture viewpoints

The repository documentation MUST include:

1. system-context view;
2. modular-monolith component view;
3. runtime/process view;
4. deployment view;
5. data-authority and provenance view;
6. security and trust-boundary view;
7. assurance and evaluation view;
8. dependency and change-impact view;
9. project and task-state view;
10. user/dashboard view.

---

# 4. Selected Mature Stack and Responsibility Boundaries

| Concern | Selected component | Contract responsibility |
|---|---|---|
| Durable agent workflow runtime | LangGraph | Checkpointed project/task graphs, parallel branches, retries, interrupts, replay, fresh subgraphs. |
| Initial workflow checkpoint store | LangGraph AsyncSqliteSaver | Lightweight single-host deadline build; non-authoritative and replaceable behind an adapter. |
| Cross-vendor model gateway | Existing LiteLLM proxy/config | Provider-neutral completion and model access; do not redesign existing provider details. |
| Authoritative graph and versioned state | TerminusDB | Contracts, requirements, projects, tasks, dependencies, decisions, evidence, provenance, assurance, KEDB, gold lineage. |
| Human project-management surface | Plane | Projects, modules, cycles, work items, owners, blockers, estimates, worklogs, dashboards; derived projection only. |
| API and modular application | Python 3.12, FastAPI, Pydantic | Typed modular-monolith API, schemas, commands, queries, middleware. Preserve existing language if an existing repository already satisfies the required interfaces. |
| Document ingestion | Docling | Structure-preserving parsing and chunk production. |
| Retrieval index | LanceDB | Derived lexical/vector/hybrid search and reranking; never authoritative. |
| RAG composition | LlamaIndex components only | Ingestion/retrieval adapters, not workflow authority or project memory. |
| Current dependency documentation | Context7 | Version-pinned documentation snapshots, refresh, caching, and diff inputs. |
| Code intelligence | git, ripgrep, Tree-sitter, language servers | Exact, structural, symbol, reference, history, and dependency inspection. |
| Evaluation runtime | Inspect AI | Sandboxed evaluation tasks, scorers, transcripts, external agents, datasets. |
| Adversarial/model testing | Promptfoo | Cross-provider behavior tests, prompt/agent attacks, CI model checks; never sole correctness authority. |
| Tracing and experiments | OpenTelemetry and Phoenix | Traces, retrieval/tool spans, evaluator annotations, experiment records. |
| Trusted release execution | GitHub Actions or existing CI | Mechanical gates, protected checks, artifact verification, PR repair, auto-merge. |
| Artifacts | Git plus content-addressed object storage | Code history, evidence bundles, large immutable artifacts. |
| Hidden evaluation | Separate verifier repository and service identity | Holdouts, private mutants, oracle internals, release scoring; inaccessible to implementers. |
| Human-facing PM projection | Plane API/webhooks/MCP | Readable project views and controlled commands, not governing truth. |

## 4.1 Explicit exclusions

- Temporal is not part of the initial critical path.
- Claude Agent SDK is not part of the permanent architecture.
- Claude Code is an optional worker adapter and temporary builder only.
- Plane is not the source of truth.
- LangGraph checkpoints are not the authoritative contract or project database.
- SQLite is used only for the initial LangGraph checkpoint store and optional local caches; it is not the project, ontology, evidence, or knowledge authority.
- LanceDB is not the ontology, KEDB authority, or provenance store.

---

# 5. Repository and Modular-Monolith Structure

The default greenfield layout shall be:

```text
src/
  api/
    routers/
    middleware/
    controllers/
    views/
  projects/
  planning/
  contracts/
  requirements/
  methodologies/
  research/
  evidence/
  dependencies/
  tasks/
  assignments/
  artifacts/
  models/
  workers/
  workflows/
  evaluation/
  oracles/
  holdouts/
  mutants/
  gold/
  knowledge/
  ontology/
  governance/
  provenance/
  drift/
  impact/
  observability/
  dashboard/
  integrations/
  composition/

tests/
  unit/
  contract/
  integration/
  e2e/
  mutation/
  architecture/

project-pack/
verifier-interface/
docs/architecture/
docs/decisions/
docs/research/
```

Each domain module SHOULD contain:

```text
module/
  domain/
  application/
  infrastructure/
  api/
  tests/
```

## 5.1 Module boundary rules

- Domain modules MUST NOT import another module’s infrastructure implementation directly.
- Cross-module operations MUST use declared application interfaces or domain events.
- All external systems MUST be behind adapters.
- A composition root MUST show how every required module is constructed and registered.
- Architecture tests MUST reject prohibited imports and circular dependencies.
- The dashboard MUST consume read projections, not mutate authoritative state directly.

## 5.2 Wiring completion rule

A module is not complete when its unit tests pass. It is complete only when it declares and proves:

```yaml
provides: []
consumes: []
startup_registration: true
configuration_schema: "..."
health_check: "..."
integration_test: "..."
e2e_path: "..."
telemetry_span: "..."
dashboard_projection: "..."
```

The composition verifier MUST fail when a module exists but is not reachable through an approved user-to-result execution path.

---

# 6. Project Pack and One-Command Experience

The owner shall be able to supply a complete project as one directory or archive:

```text
project-pack/
  contract.md
  contract.yaml
  project.yaml
  repositories.yaml
  environments.yaml
  model-policy.yaml
  methodology-policy.yaml
  dependency-policy.yaml
  autonomy-policy.yaml
  plane.yaml
  acceptance/
    visible/
    oracle-definitions/
  evidence/
    owner-documents/
    context7-snapshots/
  secrets.refs.yaml
```

The intended command is:

```bash
harness project run ./project-pack --mode autonomous
```

## 6.1 Intake behavior

The command MUST:

1. validate all schemas and references;
2. import the contract and project into an isolated TerminusDB branch;
3. inspect repositories, branches, CI, and current live state;
4. search recorded decisions and prior hard-gold/KEDB records;
5. retrieve version-pinned documentation only where needed;
6. identify unresolved material blockers;
7. ask no more than one batched owner question round before autonomous execution;
8. record answers immediately and recompile the project;
9. start the LangGraph project workflow;
10. continue until a terminal project state.

## 6.2 Terminal project states

```text
VERIFIED_COMPLETE
BLOCKED_OWNER_DECISION
BLOCKED_EXTERNAL_ACCESS
FAILED_CONTRACT
FAILED_ASSURANCE
FAILED_INFRASTRUCTURE
CANCELED
```

A worker completing a task, opening a PR, or passing visible tests is not a terminal project state.

---

# 7. Evidence-Backed Intake, Research, and Contract Formation

## 7.1 Resolver-choice order

For every open question, the system MUST apply this bounded order:

1. **Recorded decision?** Search contract, decisions, ledgers, code, and approved corpus.
2. **Objectively measurable?** Run a safe probe or benchmark.
3. **Externally researchable fact?** Retrieve primary/official/version-pinned evidence.
4. **Derivable, low-consequence, and reversible?** Derive and state the derivation.
5. **Otherwise:** raise one decision-shaped owner question.

The system MUST NOT ask the owner to resolve repository facts, recorded decisions, or safely measurable current state.

## 7.2 Fact classes

| Fact class | Primary resolver |
|---|---|
| Repository fact | Code, configuration, git history, live repository state. |
| Recorded project decision | Approved contract, decision ledger, freeze record, current policy. |
| Live empirical fact | Fresh probe, test, API check, or CI observation. |
| External technical fact | Primary research, official documentation, standards, reproducible benchmarks. |
| Owner fact | Values, scope, priorities, credentials, unpublished constraints, acceptable risk. |

## 7.3 Source assurance

Every load-bearing claim MUST record:

- source ID and URL/file pointer;
- source class and authority;
- publication/update/retrieval date;
- exact supporting location;
- direct support versus inference;
- applicability to the actual dependency/version/task;
- conflicts or missing corroboration;
- confidence and uncertainty;
- affected requirement or decision;
- content hash and retrieval provenance.

## 7.4 Hypothesis-based research and debugging

Research, architecture, and debugging MUST begin with multiple plausible hypotheses where more than one cause or design is credible.

Each hypothesis MUST include:

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

No agent may implement the first plausible fix merely because it was found first.

## 7.5 Candidate comparison

Where a material choice exists, the system SHOULD compare:

- A: current/baseline;
- B: primary candidate;
- C: known-bad or negative control;
- additional viable candidates where justified.

Selection MUST use frozen criteria and objective outcomes where available.

---

# 8. Contract Compiler and Machine-Checkable Output

The compiler MUST transform the approved contract into:

- requirement IDs and acceptance criteria;
- phase definitions and allowed transitions;
- workstreams, milestones, tasks, and work units;
- task dependencies and critical path;
- required methodologies by task and risk class;
- role definitions and incompatibility rules;
- model capability requirements;
- artifact schemas;
- allowed/prohibited repository paths;
- source and evidence rules;
- visible and hidden test obligations;
- oracle routes;
- success and failure conditions;
- contract re-review triggers;
- auto-merge conditions;
- human-escalation conditions;
- completion conditions.

## 8.1 Required phase and gate matrix

| Phase | Required outputs | Pass condition | Failure/rework condition |
|---|---|---|---|
| Project-pack validation | Validated files, resolved references, import plan | All required schemas and references validate | Missing/invalid input becomes a typed blocker; no silent defaults for material fields |
| Repository and state preflight | Repository inventory, current branch/CI state, recorded decisions | Live state is freshly observed and linked | Stale or contradictory state is reconciled before planning |
| Evidence and hypothesis planning | Research questions, source policy, hypotheses, discriminating tests | Material questions have resolvers and evidence requirements | Unsupported assumptions become `INSUFFICIENT_EVIDENCE` or a genuine blocker |
| Contract formation/freeze | Approved contract, requirements, risks, non-goals, success/failure criteria | Exact contract version is owner-authorized and committed | Ambiguity or amendment need blocks implementation |
| Project compilation | Workstreams, tasks, dependencies, roles, schemas, gates | Graph is acyclic where required, all tasks link to requirements | Unlinked work, missing gate, or circular role assignment fails compilation |
| Architecture/SDD | Module boundaries, mature dependency selections, interfaces, test plan | Architecture satisfies contract and dependency-first policy | Custom reimplementation or missing boundary returns to design |
| Walking skeleton | Real user-to-verifier path | Every required service is exercised with trace and artifact evidence | Any placeholder, unwired component, or inaccessible gate fails |
| Visible build convergence | Candidate implementation and visible tests | Visible tests and contract checks pass without unauthorized assertion weakening | Implementation/test mismatch is classified and reworked |
| Integration and composition | Startup, configuration, cross-module path, dashboard projection | Real end-to-end integration and composition verifier pass | Unit-only module or missing registration fails |
| Independent evaluation | Protected holdout, mutants, oracle results, judge findings if needed | Required holdouts pass, mutants are killed, oracle health passes | Failure routes to implementation or oracle repair without revealing protected content |
| Contract revalidation | Conformance report and impact analysis | `CONTRACT_REAFFIRMED` | Drift, stale evidence, changed risk, or amendment need routes to typed remediation |
| Deployment validation | Shadow/canary/pilot and rollback evidence when required | Risk-selected deployment gates pass | Candidate is rolled back or quarantined |
| Release and merge | Release candidate, provenance, CI gates | All auto-merge requirements pass | CI repair/rework continues; high-risk unresolved issue blocks |
| Closeout and learning | Final evidence package, KEDB/gold candidates, honest debt | Project reaches `VERIFIED_COMPLETE` | Missing evidence or unjustified promotion prevents closeout |

Every compiled object MUST contain:

```yaml
schema_id: "..."
schema_version: "..."
contract_id: "EFAH-CONTRACT-001"
contract_version: "1.0"
methodology_version: "..."
terminus_database: "..."
terminus_branch: "..."
terminus_commit: "..."
content_hash: "sha256:..."
created_by_alias: "..."
created_at: "..."
```

---

# 9. Project and Assurance Control Plane

Contract versioning alone is insufficient. The system MUST implement first-class project and assurance records.

## 9.1 Required entities

```text
Project
ProjectVersion
ProjectPack
Contract
ContractVersion
Requirement
Methodology
MethodologyVersion
Workstream
Milestone
Phase
Task
WorkUnit
Assignment
AssignmentLease
Dependency
Blocker
Decision
Assumption
Risk
ChangeRequest
Artifact
SchemaVersion
ConfigurationVersion
DependencyVersion
Environment
ModelAlias
ModelCapability
ModelRun
EvaluationRun
Oracle
OracleVersion
Holdout
Mutant
GoldCandidate
GoldCase
KnowledgeCandidate
ContractReview
ReleaseCandidate
DeploymentRun
```

## 9.2 Task ledger

The task ledger MUST maintain both:

1. append-only task events;
2. current-state projections.

Required event examples:

```text
TaskCreated
TaskReady
TaskAssigned
LeaseAcquired
LeaseRenewed
WorkerStarted
ToolCallRecorded
ArtifactSubmitted
EvaluationStarted
GatePassed
GateFailed
TaskReworked
TaskBlocked
TaskCompleted
TaskMerged
TaskClosed
```

## 9.3 Task states

```text
PROPOSED
READY
CLAIMED
RUNNING
CANDIDATE_COMPLETE
VERIFYING
PASSED
MERGED
CLOSED

BLOCKED_DEPENDENCY
BLOCKED_OWNER_DECISION
BLOCKED_EXTERNAL_ACCESS
FAILED_IMPLEMENTATION
FAILED_WIRING
FAILED_VISIBLE_TEST
FAILED_HOLDOUT
FAILED_MUTATION
FAILED_ORACLE
FAILED_SCOPE
FAILED_PROVENANCE
STALE_ASSIGNMENT
REWORK_REQUIRED
QUARANTINED
CANCELED
```

Workers may submit `CANDIDATE_COMPLETE`. Only gates may produce `PASSED`.

## 9.4 Work-unit success and failure schema

```yaml
work_unit_id: WU-0042
objective: "..."
requirement_ids: []
contract_version: "1.0"
methodology_ids: []
inputs: []
allowed_paths: []
prohibited_paths: []
required_artifacts: []
success_conditions:
  - type: command_exit
    command: "..."
    expected_exit: 0
  - type: integration_path
    path_id: "..."
  - type: hidden_holdout
    holdout_id: "..."
    expected: PASS
  - type: mutation_gate
    mutant_set: "..."
    required_kill_rate: 1.0
failure_conditions:
  - stale_contract_version
  - protected_asset_access
  - unauthorized_scope
  - missing_wiring
  - fabricated_evidence
  - unsupported_dependency_reimplementation
next_permitted_actions: []
```

## 9.5 Ownership, leases, and stale-worker fencing

Every active work unit MUST have:

- assigned role and blinded alias;
- exclusive/shared ownership mode;
- lease ID and generation;
- lease expiry and renewal policy;
- repository branch/worktree ownership;
- input hashes;
- permitted output schemas.

A submission from an expired or superseded lease MUST be rejected as stale.

## 9.6 Dependency map

The dependency graph MUST cover:

- task dependencies;
- requirement dependencies;
- artifact dependencies;
- software/package dependencies;
- service dependencies;
- documentation dependencies;
- evaluation and oracle dependencies;
- deployment/environment dependencies;
- knowledge and gold dependencies.

Required edge types include:

```text
depends_on
blocks
supported_by
derived_from
implemented_by
tested_by
verified_by
evaluated_by
invalidated_by
supersedes
compatible_with
conflicts_with
produced_by
deployed_to
```

## 9.7 Required ledgers and registries

- decision and assumption ledger;
- risk register;
- change-request ledger;
- artifact registry;
- schema/configuration registry;
- dependency registry;
- environment inventory;
- evaluation registry;
- oracle registry;
- sealed-asset registry;
- gold registry;
- knowledge/KEDB promotion ledger;
- model capability and calibration registry;
- methodology registry;
- experiment ledger;
- error-class and mechanism ledger.

## 9.8 Time tracking

Time MUST be measured from system events, not agent estimates.

Record:

```text
queued_at
claimed_at
started_at
last_heartbeat_at
candidate_submitted_at
verification_started_at
blocked_at
resumed_at
completed_at
merged_at
```

Derive queue, active, blocked, model-call, tool, evaluation, human-wait, rework, and total wall-clock durations. Plane worklogs shall display these derived values.

---

# 10. LangGraph Durable Runtime

LangGraph is the permanent workflow runtime.

## 10.1 Responsibility boundary

- TerminusDB answers: **what is true, authorized, linked, and versioned?**
- LangGraph answers: **what contract-approved executable step runs next?**
- Plane answers: **what does the human need to see and control?**
- CI/verifier answers: **did the candidate objectively pass?**

## 10.2 Required graphs

```text
project_graph
intake_graph
research_graph
contract_graph
planning_graph
build_graph
task_graph
evaluation_graph
deployment_graph
closeout_graph
contract_revalidation_graph
dependency_update_graph
```

## 10.3 Initial checkpoint profile

The deadline build SHALL use `AsyncSqliteSaver` as the initial LangGraph checkpointer when running on one host. It MUST be hidden behind a checkpoint adapter, use strict safe serialization configuration, and be treated as rebuildable execution state. If actual deployment requires multiple independent workflow worker processes writing concurrently, the adapter MAY be replaced by another officially supported durable checkpointer without changing domain schemas or project authority.

## 10.4 State requirements

Every graph checkpoint MUST reference:

- project ID;
- project version;
- contract version;
- TerminusDB database, branch, and commit;
- work unit;
- current graph/node;
- assignment lease generation;
- input and output hashes;
- pending gates and typed blockers.

## 10.5 Fresh-session subgraphs

Independent worker roles MUST use fresh per-invocation sessions by default. Persistent conversational memory is prohibited unless explicitly required by the contract.

Long-running project memory belongs in TerminusDB, artifacts, and checkpoints—not model chat context.

## 10.6 Retry and recovery

All external side effects MUST be idempotent or protected by idempotency keys.

The runtime MUST distinguish:

```text
TRANSIENT_PROVIDER_FAILURE
RATE_LIMIT
MODEL_UNAVAILABLE
WORKER_CONTEXT_LIMIT
TOOL_FAILURE
TEST_FAILURE
WIRING_FAILURE
CONTRACT_DRIFT
HOLDOUT_FAILURE
ORACLE_INVALID
PROTECTED_ACCESS
INFRASTRUCTURE_FAILURE
```

Successful parallel nodes MUST not be rerun when another node fails if their outputs were checkpointed and remain valid.

## 10.7 Human interrupts

LangGraph interrupts MAY occur only for typed owner blockers:

```text
OWNER_SCOPE_DECISION
OWNER_PRIORITY_DECISION
OWNER_RISK_ACCEPTANCE
MISSING_REQUIRED_CREDENTIAL
IRREVERSIBLE_EXTERNAL_ACTION
CONTRACT_AMENDMENT_REQUIRED
IRRESOLVABLE_EVIDENCE_CONFLICT
```

Routine implementation, retry, fallback, test repair, PR creation, or green auto-merge MUST NOT create an owner interrupt.

---

# 11. Model Router, Middleware, Controllers, and Views

## 11.1 Model router

The model router is a deterministic policy service, not an orchestrator.

It receives:

```yaml
role: "implementation_worker"
required_capabilities: []
prohibited_aliases: []
required_family_separation: true
risk_class: "..."
context_requirement: "..."
availability_probe_required: true
```

It uses:

- the existing LiteLLM configuration;
- approved model aliases;
- current empirical availability;
- capability and calibration records;
- role incompatibility rules;
- provider/family separation;
- task risk;
- context and tool requirements;
- retry and fallback policy.

It returns an alias and configuration version. Other agents never receive the real vendor/model identity.

## 11.2 Protected identity mapping

Real model identity mappings MUST be stored in a separate protected TerminusDB database or isolated instance.

Normal task and audit records MUST use aliases. The owner MUST be able to reveal the mapping later for audit and performance analysis.

## 11.3 API router

The HTTP/API router maps endpoints to controllers only. It MUST NOT contain workflow or model-routing decisions.

Representative endpoints:

```text
POST /projects/import
POST /projects/{id}/run
GET  /projects/{id}/status
GET  /projects/{id}/graph
GET  /projects/{id}/scope-drift
GET  /tasks/{id}
POST /tasks/{id}/resume
GET  /evaluations/{id}
GET  /dependencies/{id}/impact
POST /contracts/{id}/approve
POST /contracts/{id}/review
```

## 11.4 Middleware

Middleware shall handle:

- authentication and authorization;
- human, service, and alias identity;
- contract/project version binding;
- correlation and trace IDs;
- schema validation;
- request provenance;
- rate and concurrency controls;
- input limits;
- audit logging;
- prompt-injection and untrusted-content boundaries.

## 11.5 Controllers

Controllers translate commands into application use cases. They MUST NOT contain persistence-specific code, model prompts, or hidden evaluator logic.

## 11.6 Dashboard views

The dashboard MUST show:

1. project and milestone status;
2. task ledger and critical path;
3. task ownership, leases, worktrees, and stale sessions;
4. contract/requirement traceability;
5. scope-drift findings;
6. model-run aliases and role history;
7. visible/hidden evaluation status without exposing protected content;
8. oracle health and mutant results;
9. dependency versions and impact maps;
10. knowledge and hard-gold promotion state;
11. provenance graph;
12. release readiness;
13. exact typed blocker and requested owner decision, if any.

Plane shall receive these as controlled projections. A built-in minimal dashboard MAY supplement Plane but MUST not replace the required authoritative control plane.

---

# 12. Cross-Vendor Roles and Non-Circular Validation

## 12.1 Roles

Supported roles include:

- researcher;
- research challenger;
- planner;
- plan challenger;
- visible-test author;
- sealed-holdout author;
- mutant author;
- oracle author;
- implementer;
- integration/wiring verifier;
- adversarial critic;
- judge;
- evidence auditor;
- contract-compliance auditor;
- release verifier.

## 12.2 Separation rules

- Builder, holdout author, and final adjudicator MUST be distinct roles and agents.
- The implementer MUST NOT access sealed holdouts, private mutants, or oracle internals.
- A producing model MUST NOT be the sole reviewer or judge of its output.
- Same-family validation MUST be rejected where family bias is material and a cross-family alternative is available.
- Model judgment MUST NOT replace a deterministic oracle.
- Cross-vendor agreement is evidence, not proof.

## 12.3 Blinded operation

Agents see aliases only, such as:

```text
researcher-r17
planner-p04
implementer-i12
critic-c08
judge-j03
```

No agent may receive another agent’s vendor, model family, prestige ranking, or cost tier unless required for a protected routing audit unavailable to task participants.

## 12.4 Produce, critique, adjudicate

For high-stakes judgment:

1. a producer creates an artifact with evidence;
2. an independent cross-family critic tries to refute it;
3. a separate adjudicator resolves each dispute with evidence, an oracle, or escalation;
4. corrections remain visible in lineage;
5. an uncalibrated judge’s result is advisory only.

## 12.5 Blind convergence

For an open high-stakes design fork, two different-vendor frontier models MAY receive the same self-contained problem independently. Agreement on both verdict and core invariant raises confidence but MUST NOT alone pass a gate when an experiment or deterministic check is available.

---

# 13. Methodology Catalog and Mechanical Enforcement

The stale Cortex methodology map is a candidate source, not direct contract authority. Adopted methods MUST be normalized into a versioned methodology catalog.

## 13.1 Methodology categories

Each methodology must be classified as:

- contractual invariant;
- operating procedure;
- configurable policy;
- measured capability rule;
- experimental recommendation;
- obsolete/superseded rule.

## 13.2 Required methodology themes

The initial catalog MUST cover:

- mechanism over memory;
- bounded recorded-decision search;
- resolver choice;
- decision-shaped owner elicitation;
- blind implementation/test convergence;
- sealed holdout and mutant verification;
- multi-model critique and adjudication;
- governed amendment/freeze;
- closeout and durable capture;
- deterministic model dispatch policy;
- measured-not-guessed benchmarking;
- honest debt and provenance;
- subagent briefing and independence checks;
- blocked-state behavior;
- fresh-observation reporting;
- handoff reconciliation;
- one-step refutation;
- convenience-gradient correction;
- error-to-mechanism conversion;
- per-model judge calibration;
- oracle minting;
- deep audit;
- citation discipline;
- question quality;
- legible output.

## 13.3 Applicability compiler

The contract compiler MUST select required methodologies from task class and risk. Agents shall not manually decide which methods “feel relevant.”

Example:

```yaml
task_class: trust_critical_code_change
risk: high
required_methodologies:
  - recorded_decision_preflight
  - blind_build_lane
  - sealed_holdout
  - closeout_capture
  - honest_debt
  - oracle_minting
conditional:
  external_research:
    - deep_research
  disputed_design:
    - multi_model_arbitration
  tunable_selection:
    - measured_benchmarking
```

## 13.4 Mechanization rule

If an owner correction or repeated failure establishes a material rule, the system MUST create or update:

- governing artifact;
- task/dispatch pointer;
- schema/gate/hook/policy where possible;
- incident/error-class record;
- regression test or hard-gold candidate.

---

# 14. SDD, TDD, Walking Skeleton, and Wiring

## 14.1 Software Design Document

A task requiring material implementation MUST have a versioned SDD covering:

- problem and constraints;
- contract requirements;
- selected mature dependencies;
- rejected alternatives;
- module boundaries;
- data and control flow;
- interfaces and schemas;
- failure and recovery behavior;
- security boundaries within approved scope;
- test and evaluation strategy;
- rollout and rollback;
- honest debt.

## 14.2 Dependency-first gate

Before custom implementation, the agent MUST produce a `BUILD_VS_INTEGRATE` record:

```yaml
capability: "..."
existing_candidates: []
selected_dependency: "..."
version: "..."
why_adapter_is_sufficient: "..."
custom_code_required: "..."
rejected_reimplementation: true
```

Custom infrastructure duplicating a mature selected dependency MUST fail the scope gate.

## 14.3 Test-first behavior

Visible acceptance and contract tests SHALL be authored independently from the implementation when the risk classifier requires it.

Visible behavioral assertions MUST be hashed before convergence. A change to assertions requires an explicit test amendment linked to the contract.

## 14.4 Walking skeleton

Before broad implementation, the system MUST prove:

```text
project-pack import
→ TerminusDB project/contract commit
→ LangGraph project run
→ task creation and Plane projection
→ model alias routing through LiteLLM
→ fresh worker session
→ tool/repository action
→ artifact submission
→ trace and provenance
→ visible test
→ protected verifier call
→ oracle result
→ CI gate
→ dashboard update
```

## 14.5 Eval-first milestone

The Eval Lab skeleton, protected verifier interface, and at least one real hidden gate MUST exist before broad feature expansion. The Eval Lab may not be deferred until after the application modules are built.

---

# 15. RAG, Living Ontology, KEDB, and Verified Learning

## 15.1 Separate retrieval planes

The system MUST separate:

1. research evidence;
2. code intelligence;
3. operational KEDB;
4. project/contract knowledge;
5. gold calibration;
6. diagnostic holdouts;
7. release holdouts.

Protected evaluation planes MUST use separate credentials and no implementer retrieval route.

## 15.2 TerminusDB authority

TerminusDB is authoritative for:

- entities and relationships;
- document versions and provenance;
- claims and evidence;
- project/task/dependency state;
- contract and requirement lineage;
- KEDB promotion status;
- gold lineage;
- invalidation relationships.

Every material write MUST create an attributable immutable commit. Candidate changes MUST occur on isolated branches and merge only after applicable gates.

## 15.3 Derived retrieval index

Every LanceDB row MUST resolve to:

```yaml
terminus_database: "..."
terminus_branch: "..."
terminus_commit: "..."
document_id: "..."
document_version: "..."
content_hash: "..."
visibility_class: "..."
trust_tier: "..."
embedding_model: "..."
embedding_version: "..."
parser_version: "..."
```

The index MUST be rebuildable from authoritative documents and graph state.

## 15.4 Retrieval pipeline

```text
question classification
→ permitted corpus/trust tiers
→ lexical retrieval
→ dense retrieval
→ rank fusion
→ reranking
→ source diversity
→ contradiction retrieval
→ exact evidence packet
→ citation and claim validation
```

The retriever MUST be able to return `INSUFFICIENT_EVIDENCE`.

## 15.5 Knowledge tiers

```text
T0 RAW
T1 OBSERVATION
T2 HYPOTHESIS
T3 TESTED
T4 REPRODUCIBLE
T5 INDEPENDENTLY_VERIFIED
T6 APPROVED_OPERATIONAL_KNOWLEDGE
T7 HARD_GOLD
```

Unverified agent output MUST NOT be presented as trusted knowledge.

## 15.6 Gold promotion

A successful task becomes a hard-gold candidate only when the system preserves:

- original contract and specification;
- initial environment and versions;
- expected and observed results;
- tests and oracles;
- artifacts and hashes;
- traces and tool calls;
- independent verification;
- failure variants and mutants;
- contamination and trainability policy.

Promotion requires quarantine, reproducibility, independent verification, mutant validation, and contamination review.

## 15.7 Automatic ontology updates and invalidation

A changed source, dependency, schema, contract, oracle, or environment MUST trigger graph impact analysis. Affected evidence, decisions, requirements, tasks, tests, gold cases, and release candidates MUST be marked stale or scheduled for revalidation rather than silently remaining trusted.

---

# 16. Context7 and Dependency Lifecycle

The two Context7 credentials are operational capacity/failover credentials, not independent evidence sources.

## 16.1 Snapshot requirements

Each retrieval MUST record:

```yaml
snapshot_id: C7-...
credential_alias: "primary|secondary"
library_id: "..."
library_version_or_branch: "..."
query: "..."
retrieved_at: "..."
raw_response_hash: "sha256:..."
normalized_response_hash: "sha256:..."
source_locations: []
affected_dependencies: []
affected_decisions: []
```

## 16.2 Version-diff loop

```text
new dependency version detected
→ retrieve/refresh pinned documentation
→ cache and hash snapshot
→ diff API/configuration/behavior documentation
→ query dependency graph
→ create dependency-update task
→ update isolated branch
→ run unit, integration, mutation, gold, shadow/canary gates
→ merge or reject with evidence
```

Automatic discovery and candidate preparation are allowed. Automatic merge is allowed only under the configured risk and gate policy.

## 16.3 Dependency registry

Every dependency MUST record:

- exact version and lockfile source;
- image digest where applicable;
- documentation snapshot;
- configuration hash;
- modules and contracts using it;
- known compatibility constraints;
- last verified run;
- update and rollback policy;
- affected gold and integration tests.

---

# 17. Eval Lab and Protected Verifier

## 17.1 Evaluation sets

- development tests;
- visible contract tests;
- diagnostic hidden tests;
- sealed release holdouts;
- hard-gold cases;
- fresh challenge cases;
- implementation mutants;
- test mutants;
- evaluator/oracle mutants;
- workflow/governance mutants.

## 17.2 Protected verifier architecture

The protected verifier MUST be in a separate repository and/or service identity that implementation workers cannot read, list, clone, query, or modify.

The candidate system may submit only:

- artifact/commit identifier;
- allowed runtime inputs;
- evaluation request ID;
- required contract/oracle version.

The verifier returns only the contract-approved result shape. It MUST not reveal hidden assertions, private fixtures, or mutant implementation details.

## 17.3 Oracle hierarchy

1. exact deterministic execution/state oracle;
2. static/AST/type/policy checker;
3. property, differential, or metamorphic test;
4. reference implementation;
5. reproducible empirical benchmark;
6. calibrated model judge;
7. owner adjudication.

An available higher-level deterministic oracle MUST not be replaced by a lower-level subjective one.

## 17.4 Oracle minting

A trusted oracle MUST have:

- a deterministic verdict path with no hidden model call;
- structural proof that no judge participates in the verdict path;
- independent second-checker comparison where feasible;
- known-good and known-bad fixtures;
- gaming probes;
- mutants that it kills;
- honest `UNVERIFIABLE` output where it cannot decide;
- a pinned checker test suite;
- version and content hash;
- last audit date;
- health emitted with every result.

## 17.5 Judge calibration

Model judges are qualified per:

```text
model version × domain × rubric version
```

Record:

- sample size;
- gold provenance;
- Cohen’s kappa or selected agreement measure;
- exact accuracy;
- false-pass and false-fail rates;
- abstention rate;
- confusion matrix;
- confidence interval where possible;
- family-bias/confound notes;
- recalibration triggers.

Uncalibrated judge output is advisory.

## 17.6 Evaluation modes

The contract/risk classifier assigns black-box, gray-box, or white-box access per phase and evaluator. Access is explicit and least-privileged.

---

# 18. Mechanical Verification and Provenance

The mechanical verifier MUST check, at minimum:

| Object | Required checks |
|---|---|
| Contract | ID, version, approval, schema, hash, lineage. |
| Task | Linked requirements, allowed scope, dependencies, lease, state transition. |
| Model run | Alias, protected identity reference, eligible role, configuration hash, input/output hashes. |
| Tool call | Tool identity, arguments, result, exit code, permission, trace link. |
| Repository change | Repository, branch, base/head commit, paths changed, prohibited-path violations. |
| Test | Command, environment, timestamp, exit status, raw result artifact, commit binding. |
| Evaluation | Target artifact, evaluator/oracle version, environment, holdout visibility, verdict provenance. |
| Artifact | Content hash, producer, source inputs, contract/task links, storage location. |
| Release | Exact commit, artifact digest, gate results, provenance/attestation, deployment evidence. |

Every result MUST carry an evidence/provenance tier and honest debt:

```text
OWNER_VERIFIED
DETERMINISTIC_ORACLE
INDEPENDENTLY_REPRODUCED
CALIBRATED_MODEL_VERIFIED
AI_DISCOVERED_UNVERIFIED
```

“Done” without named evidence is invalid.

---

# 19. Scope Drift, Contract Re-Review, and Change Control

## 19.1 Continuous scope comparison

The drift engine MUST compare:

```text
approved contract
vs compiled requirements
vs project plan
vs active tasks
vs changed artifacts
vs test/evaluation claims
vs release contents
```

## 19.2 Drift finding types

```text
UNLINKED_TASK
UNAPPROVED_SCOPE_EXPANSION
REQUIREMENT_WEAKENING
REDEFINED_SUCCESS
OUTSIDE_ALLOWED_PATHS
STALE_CONTRACT_VERSION
STALE_INPUT_ARTIFACT
DUPLICATE_OR_CONFLICTING_WORK
ROLE_CONFLICT
PROTECTED_ASSET_ACCESS
MISSING_WIRING
UNSUPPORTED_REIMPLEMENTATION
OUT_OF_SCOPE_SECURITY_EXPANSION
```

## 19.3 Contract re-review frequency

The project pack MUST contain `contract_review_interval_phases`. Default: **3 material phases** if omitted.

A contract-conformance review MUST also run:

- before implementation;
- after the walking skeleton;
- before sealed evaluation;
- before shadow/canary/pilot;
- before release;
- after a material dependency, source, schema, risk, oracle, or design change;
- after repeated failures indicating a potentially wrong plan;
- after any holdout exposure or role-separation violation.

## 19.4 Review outcomes

```text
CONTRACT_REAFFIRMED
DRIFT_DETECTED
EVIDENCE_STALE
RISK_CHANGED
CONTRACT_AMBIGUITY
AMENDMENT_REQUIRED
```

Only `CONTRACT_REAFFIRMED` advances automatically. Review is conformance checking, not an invitation to add optional improvements.

## 19.5 Security-review scope boundary

A security finding blocks only when it:

- maps to an approved requirement, threat, risk, or policy;
- provides concrete evidence or an executable exploit/probe;
- states the smallest compliant remediation.

Other findings become `OUT_OF_SCOPE_OBSERVATION` and do not expand the build.

---

# 20. Human-in-the-Loop and Question Policy

The system is designed to reduce human intervention over time.

## 20.1 Human review surface

Human review should focus on:

- initial contract approval;
- genuine scope/value/risk decisions;
- irreversible actions;
- final output, wiring, trust boundaries, and evidence package;
- unresolved high-impact conflicts.

Human line-by-line code review is not required where mechanical and independent assurance is adequate.

## 20.2 Question budget

After initial project-pack intake, the system may ask one batched question round containing only genuine consequential forks.

Each question MUST include:

- what it blocks;
- 2–4 concrete options;
- consequence of each option;
- evidence;
- recommendation and confidence, shown after neutral options for high-impact decisions.

The system MUST NOT drip questions across phases when they could have been batched.

## 20.3 Automatic continuation

When a task fails within contract, the system MUST route rework automatically. It MUST NOT ask the owner whether to fix ordinary test, integration, or CI failures.

---

# 21. CI/CD, Pull Requests, and Auto-Merge

The project MUST define:

```yaml
autonomy:
  continue_without_human_confirmation: true
  auto_open_pr: true
  auto_repair_ci: true
  auto_merge: true
```

## 21.1 Required gate sequence

```text
schema validation
→ contract/version binding
→ role/lease validation
→ scope and architecture checks
→ static/type/unit tests
→ contract and integration tests
→ composition/wiring test
→ visible evaluation
→ protected holdout
→ mutation gates
→ oracle-health gate
→ provenance and artifact verification
→ dependency policy
→ release readiness
→ auto-merge
```

## 21.2 Auto-merge conditions

```yaml
auto_merge_requirements:
  contract_unchanged_or_approved: true
  unresolved_scope_drift: 0
  visible_tests: PASS
  integration_tests: PASS
  composition_test: PASS
  hidden_holdout: PASS
  mutation_gate: PASS
  oracle_health: PASS
  provenance_gate: PASS
  dependency_policy: PASS
  unresolved_high_risk_findings: 0
  protected_assets_accessed: false
  branch_up_to_date: true
```

CI or an approved service identity performs the merge. The implementing agent does not self-certify.

A green, mergeable PR under an active auto-merge policy MUST NOT wait for additional human permission.

---

# 22. Shadow, Canary, Pilot, and Operational Validation

Operationally material changes MUST use the risk-selected progression:

```text
offline evaluation
→ shadow
→ canary
→ pilot
→ broader rollout
```

Candidate comparison should consider:

- correctness;
- reliability;
- failure containment;
- rollback;
- security within approved scope;
- maintainability;
- operational complexity;
- performance;
- compatibility;
- long-term contract compliance.

The quickest patch is not automatically selected.

---

# 23. Observability and Dashboard Evidence

Every project, task, model call, retrieval, tool call, evaluation, and gate MUST emit correlated OpenTelemetry traces.

Minimum correlation fields:

```text
project_id
contract_version
task_id
work_unit_id
run_id
model_alias
role
terminus_commit
repository_commit
evaluation_id
oracle_version
trace_id
```

Phoenix SHALL provide trace inspection and experiment/evaluator views. Plane SHALL present project state, ownership, timing, blockers, and readiness. Neither may become the trusted verdict authority.

---

# 24. Three-Day Build Plan

The builder MUST optimize for the end-to-end path rather than the number of isolated modules.

## Day 1: Control-plane spine and walking skeleton

Required outcomes:

- final repository structure and architecture rules;
- Docker/dev environment for TerminusDB, Plane, LiteLLM connection, retrieval, and observability;
- contract/project schemas;
- TerminusDB main and protected schemas;
- project/task/dependency/assignment/artifact/evaluation registries;
- LangGraph project and task skeletons;
- Plane projection adapter;
- Context7 snapshot cache;
- one model router path;
- CI pipeline with failing placeholder gates replaced by executable skeleton gates;
- first complete walking-skeleton run.

## Day 2: Workers, RAG, project automation, and Eval Lab

Required outcomes:

- fresh worker adapters through LiteLLM;
- temporary Claude Code worker adapter only if useful;
- code intelligence;
- Docling/LanceDB evidence retrieval;
- hypothesis/research workflow;
- task leases and parallel worktrees;
- Inspect task and protected verifier interface;
- visible tests, sealed holdout, mutants, and one deterministic oracle;
- OTel/Phoenix tracing;
- scope-drift and contract-review workflows;
- automatic PR/CI repair path.

## Day 3: Prove autonomy and close out

Required outcomes:

- run one representative project through the whole system;
- run multiple fresh cross-vendor roles;
- prove no module-wiring omission;
- prove stale worker rejection;
- prove scope-drift rejection;
- prove protected holdout isolation;
- prove a known-bad mutant fails;
- prove restart/resume without project reset;
- prove green auto-merge;
- promote at least one verified KEDB item and one hard-gold candidate;
- produce final evidence package and honest debt.

## Delivery rule

Do not spend the deadline polishing optional UI or broadening task families while a required end-to-end gate remains unwired.

---

# 25. Acceptance Test

The build is accepted only when all of the following are demonstrated with artifacts and traces.

1. A project pack imports into an isolated TerminusDB branch.
2. All supplied schemas validate and are version-bound.
3. The system asks no unnecessary owner questions and batches genuine blockers.
4. The contract compiles into a project/task/dependency graph.
5. LangGraph resumes a failed/interrupted project from checkpoint without restarting completed work.
6. Fresh worker sessions execute bounded tasks through LiteLLM.
7. Real model identities remain hidden from task participants and visible to the owner audit path.
8. Plane shows project, task, owner, blocker, dependency, timing, scope-drift, evaluation, and release state.
9. Parallel agents use separate leases and worktrees; a stale worker submission is rejected.
10. A module with unit tests but missing composition wiring fails acceptance.
11. A mature dependency is integrated through an adapter and equivalent custom reimplementation is rejected.
12. The research path records competing hypotheses and a discriminating test.
13. Context7 version-pinned documentation is cached, hashed, and linked to a dependency decision.
14. A documentation/dependency change produces an impact map and revalidation task.
15. The RAG index resolves every result to a TerminusDB commit and source hash.
16. Unverified agent knowledge remains below trusted KEDB tiers.
17. The protected verifier is inaccessible to the implementer.
18. A visible suite, hidden holdout, and mutant suite all execute against the same candidate commit.
19. A known-bad implementation or workflow mutant is rejected.
20. The oracle emits version and health and has no model judge in its deterministic verdict path.
21. Scope drift and unauthorized cybersecurity expansion are detected and blocked without expanding the contract.
22. Contract re-review occurs after the configured interval and at an event trigger.
23. Tool calls, repository changes, tests, traces, and artifacts are mechanically bound to the exact contract and commit.
24. A green PR is automatically merged without waiting for an additional human message.
25. The final system returns `VERIFIED_COMPLETE` with a complete evidence package.

---

# 26. Failure-Specific Required Mechanisms

| Observed failure | Required correction |
|---|---|
| Scope drift | Requirement-linked tasks, allowed paths, continuous drift engine, periodic/event contract reviews. |
| Too many questions | Resolver-choice gate, recorded-decision search, safe measurement, one batched blocker round. |
| Modules built but not wired | Walking skeleton first, composition graph, startup registration, integration/e2e gate. |
| Security agents expand scope | Frozen threat model, requirement-linked findings, out-of-scope observation state. |
| Agents rebuild mature dependencies | Mandatory build-vs-integrate gate and adapter-first architecture. |
| Contract/scope not tracked | Contract version bound to every task, run, artifact, test, gate, and release. |
| Eval harness never completed | Eval Lab and protected verifier are early milestones and acceptance blockers. |
| Human loses project state | Plane dashboards plus authoritative task, decision, dependency, and assurance ledgers. |
| Long-running harness never finishes | LangGraph durable workflow, typed terminal states, retry/rework loops, auto-merge. |
| Agent waits after green PR | Explicit autonomous continuation and CI service-account auto-merge. |
| Circular testing | Separate test/holdout/mutant/oracle roles and protected verifier repository. |
| Context rot | Fresh bounded sessions; durable state in TerminusDB, LangGraph checkpoints, Git, and artifacts. |
| Repo becomes harder to repair than restart | Idempotent workflow, isolated branches/worktrees, checkpoint recovery, schema and architecture gates. |

---

# 27. Final Evidence Package

A successful run MUST produce:

```text
Project status: VERIFIED_COMPLETE
Project ID and version
Contract ID and version
TerminusDB database/branch/commit
Release repository and commit
Release artifact digest
Requirements satisfied / total
Tasks completed / total
Visible tests result
Integration/composition result
Hidden holdout result
Mutants seeded/killed
Oracle versions and health
Scope-drift findings and resolution
Dependency versions and verification status
Deployment/shadow/canary evidence where required
Knowledge promotions
Hard-gold candidates/promotions
Model aliases and protected audit references
Timing breakdown
Auto-merged PR reference
Honest debt and deliberately deferred non-goals
```

A final prose summary without this package is not completion.

---

# 28. Explicit Non-Goals for the Deadline Build

- exhaustive support for every programming language and repository type;
- enterprise-scale multi-region high availability;
- universal formal verification;
- complete automated remediation of every dependency update;
- a large calibrated gold corpus for every domain;
- fully autonomous irreversible production operations without policy approval;
- replacing Plane, TerminusDB, LangGraph, LiteLLM, Inspect, or other selected mature dependencies with custom equivalents;
- UI polish that delays the real end-to-end workflow.

---

# 29. Inputs Claude Code May Treat as Genuine Blockers

Claude Code may request only missing information that cannot be discovered or safely defaulted, such as:

- repository URL/path and branch when not supplied;
- required credentials or service endpoints;
- deployment target where no existing environment is discoverable;
- owner values, risk acceptance, or irreversible-action authorization;
- a material conflict between this contract and another approved contract.

Repository structure, dependency versions, tests, code behavior, CI state, and existing configuration must be inspected rather than asked about.

---

# 30. Approval and Version Record

| Version | Date | Status | Summary |
|---|---|---|---|
| 0.1 | 2026-08-01 | Superseded draft | Initial durable contract. |
| 0.2 | 2026-08-01 | Superseded draft | TerminusDB selected as initial graph authority. |
| 1.0 | 2026-08-01 | Final execution candidate | LangGraph permanent runtime; modular-monolith control plane; Plane projection; project/task/dependency/assurance ledgers; scope-drift and contract reviews; autonomous CI/merge; fresh vendor-neutral sessions. |

**Owner execution act:** Supplying this file to Claude Code with an instruction to execute it authorizes implementation under version 1.0 without additional scope confirmation, subject only to the typed blocker rules in this contract.

---

# Appendix A. Project State Overview

```text
Project
  ├── ContractVersion
  ├── Workstreams
  │     ├── Milestones
  │     │     ├── Phases
  │     │     │     ├── Tasks
  │     │     │     │     ├── WorkUnits
  │     │     │     │     ├── Assignments/Leases
  │     │     │     │     ├── Artifacts
  │     │     │     │     └── Evaluations/Gates
  ├── Dependencies
  ├── Risks/Decisions/Changes
  ├── Oracles/Holdouts/Mutants/Gold
  ├── Knowledge/KEDB
  └── ReleaseCandidates/Deployments
```

# Appendix B. End-to-End Runtime

```text
User project pack
→ validation and import
→ evidence/repository preflight
→ one blocker question round if required
→ contract compilation
→ TerminusDB commit
→ LangGraph project graph
→ Plane project projection
→ task dependency scheduling
→ blinded model route through LiteLLM
→ fresh sandbox/worktree session
→ candidate artifact
→ mechanical scope/wiring verification
→ visible tests
→ protected verifier
→ holdout/mutants/oracle
→ CI/release gates
→ auto-merge
→ knowledge/gold promotion
→ final evidence package
```

# Appendix C. Official Technical References

Retrieved and verified on 2026-08-01:

1. LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
2. LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
3. LangGraph subgraphs: https://docs.langchain.com/oss/python/langgraph/use-subgraphs
4. LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
5. LangGraph checkpointers: https://docs.langchain.com/oss/python/integrations/checkpointers
6. Plane developer documentation: https://developers.plane.so/
7. Plane API introduction: https://developers.plane.so/api-reference/introduction
8. Plane work items: https://developers.plane.so/api-reference/issue/overview
9. Plane worklogs: https://developers.plane.so/api-reference/worklogs/overview
10. TerminusDB version control: https://terminusdb.org/docs/version-control-operations/
11. TerminusDB schema: https://terminusdb.org/docs/schema-reference-guide/
12. TerminusDB document graph model: https://terminusdb.org/docs/documents-explanation/
13. LiteLLM documentation: https://docs.litellm.ai/
14. Context7 API guide: https://context7.com/docs/api-guide
15. Context7 library updates: https://context7.com/docs/library-updates
16. Context7 GitHub Actions refresh: https://context7.com/docs/integrations/github-actions
17. Inspect AI documentation: https://inspect.aisi.org.uk/
18. Promptfoo documentation: https://www.promptfoo.dev/docs/
19. Phoenix documentation: https://arize.com/docs/phoenix

