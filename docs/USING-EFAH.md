# Using EFAH — owner's manual

**Written:** 2026-08-02 · **Contract:** EFAH-CONTRACT-001 v1.1

This is the practical guide: what to open, which mode does which work, where the
logs are, and what is not built yet. It documents what exists on 2026-08-02, and
says so plainly where something is missing — a manual that describes intended
behaviour rather than measured behaviour is how a build starts lying to itself.

---

## 1. The two surfaces

There are two, and they are not interchangeable.

| | Chat UI | Owner control surface |
|---|---|---|
| URL | `http://gravebuster.tail733a0f.ts.net:8095` | `http://gravebuster.tail733a0f.ts.net:8088/owner/` |
| What it is | Open WebUI, pointed at the harness | The §11.7 control plane |
| Shape | One turn per message | Verbs against project state |
| Use it for | Thinking, planning, reviewing, one work unit at a time | Starting and steering long-running autonomous work |
| Auth | Account (first signup became admin) | Tailnet only |

**Chat is one turn per message.** It cannot run a multi-hour build on its own.
That is what the control surface plus the instruction consumer are for.

Both ride the tailnet only. Neither is exposed to the public internet, and the
chat UI reaches your phone through `tailscale serve` — a raw bound port does not.

---

## 2. Modes — what each one actually does

Five modes exist, defined in `src/orchestration/modes.py`. Pick one from the
model dropdown in the chat UI.

| Mode id | Role it runs as | Budget | Graph | What it is for |
|---|---|---|---|---|
| `efah-auto` | implementer | 2048 | — | One turn, answered directly. The default |
| `efah-plan` | planner | 4096 | planning | Decompose into work units with dependencies. **Does not execute** |
| `efah-research` | researcher | 6144 | intake | Evidence-first; load-bearing claims carry a source |
| `efah-review` | adversarial_critic | 4096 | — | Tries to refute. Cross-family from the implementer by §12.4 |
| `efah-build` | implementer | 8192 | build | Runs one work unit; produces a **candidate**, never a certification |

The role is what matters. Each mode dispatches under a contract role, so the
router's separation rules, the gateway split, prohibited models and the
availability requirement all apply to chat exactly as they apply to the graphs.
**You never choose a model.** Asking for `gpt-4o` is refused by design — the
whole point of the façade is that a chat client cannot bypass the gates by
naming a model.

### The working loop

```
efah-research   →  gather evidence, with sources
efah-plan       →  decompose into work units
efah-build      →  take ONE work unit, get a candidate diff
efah-review     →  refute it, from a different vendor family
```

`efah-review` matters more than it looks. §12.4 forbids a producing model from
being the sole reviewer of its own output, and the critic seat is deliberately
cross-family from the implementer. Pasting a candidate into review is not a
formality; it is the only independent check chat gives you.

### Mechanics that will surprise you

- **The first user message of a chat defines the thread.** The LangGraph thread
  id is derived from it, so the same opening text always resolves to the same
  thread. **Start a new chat for new work.** Continuing an old chat resumes that
  thread's state.
- **Only the last 20 turns are carried.** A long conversation silently loses its
  beginning (`MAX_HISTORY_MESSAGES` in `src/api/routers/chat.py`).
- **Your message is data, not instructions.** The conversation is wrapped in a
  DATA envelope before dispatch, so a message saying "ignore previous
  instructions" is treated as text to reason about, not as a command.
- **`efah-build` will not touch the repository.** §21.2 forbids the implementing
  agent from self-certifying. You get a diff to review.
- **A refusal is not an outage.** If the harness refuses on policy you get
  readable text explaining which rule fired, with a normal `finish_reason`, not
  an error page.

### Files

Attach documents and zip archives freely — verified working end to end.

- **Works:** `.txt`, `.md`, `.json`, `.py`, `.yaml`, `.html`, `.log`, `.rst`,
  `.csv`, `.tsv`, and `.zip` (including nested directories and multi-file
  archives). Answers come back with `[1]` citation markers.
- **Known trap:** a **single-line CSV extracts to nothing**. The loader reads the
  only line as a header with no data rows, and the upload contributes nothing to
  the answer. Give a CSV a header *and* at least one data row.
- **Extraction is asynchronous.** An upload is `pending` for about a second
  before its content is indexed. Attaching and sending instantly can race it.

---

## 3. Long-running work — the control surface

Chat cannot do this. The control surface at `:8088/owner/` has exactly six verbs
(§11.7), and `efah-instruction-consumer` is the service that acts on them.

| Verb | What it does |
|---|---|
| `OBSERVE` | Read project and task state |
| `INSTRUCT` | Issue a contract-bounded instruction. **This starts real work** |
| `ANSWER_BLOCKER` | Answer an open typed blocker when work stops and asks |
| `RESUME` | Continue a stopped work unit |
| `RETRY` | Re-attempt a failed work unit |
| `CANCEL` | Stop a work unit |

Everything except `OBSERVE` mutates, and **none of them self-approve** — each
still enters the normal gate path. The surface will refuse a command with a
typed reason rather than quietly doing something narrower:

`UNAPPROVED_SCOPE_EXPANSION` · `GATE_BYPASS_ATTEMPTED` ·
`PROTECTED_ASSET_ACCESS` · `CONTRACT_AMENDMENT_REQUIRED` · `UNKNOWN_TARGET` ·
`NOT_A_PERMITTED_VERB`

> **Not re-verified.** HANDOFF-003 records the instruction consumer driving real
> work end to end via systemd, and the service is running. That path was not
> re-measured on 2026-08-02, so treat it as a recorded claim rather than a
> confirmed one until an `INSTRUCT` is run through it.

---

## 4. Contract making

**Neither surface writes the contract.** The contract is owner data in
`project-pack/`, and the refusal reasons above exist specifically to stop chat
from expanding it. GATE-D1-10 A6 and A7 verify that a scope expansion is
*rejected rather than executed*, and that the surface cannot bypass a gate or
self-approve.

Changing the contract means editing the pack yourself and re-running validation.
That friction is deliberate: a system that can rewrite its own contract from a
chat box does not have a contract.

---

## 5. Where the logs actually are

| What | Where | Command |
|---|---|---|
| Chat + façade service | systemd journal | `journalctl --user -u efah-owner-surface -f` |
| Instruction consumer | systemd journal | `journalctl --user -u efah-instruction-consumer -f` |
| Open WebUI | container | `docker logs -f efah-openwebui` |
| **Every owner command** | append-only ledger | `tail -f .data/owner_surface_ledger.jsonl` |
| Gate results | evidence | `evidence/gate-run-summary.json` |
| Model probes | evidence | `evidence/model-requalification*.json`, `evidence/generation-config-sweep*.json` |
| Traces | Phoenix, EFAH's own instance | `http://localhost:6007` |

The **owner ledger** is the one worth knowing. Every command through the surface
is appended with a provenance envelope — content hash, contract id and version,
the alias that created it, the verb, whether it was accepted, and the rejection
reason if not. It is the audit trail for anything you told the system to do.

Phoenix is a **dedicated** instance on port 6007, deliberately not the
`cortex-phoenix` on 6006; `environments.yaml` records
`must_not_share_instance_with: [cortex-phoenix, cortex-otel-collector]`, and the
OTel adapter refuses a forbidden endpoint at construction time. OTLP export goes
to `localhost:4319`.

**The eval gateway's own logs are not local.** `litellm_eval` runs on the owner's
Railway deployment; the harness records what it sent and received in
`evidence/`, but the gateway-side logs live in Railway.

---

## 6. Is it done? No — and here is the number

As of 2026-08-02, from `evidence/gate-run-summary.json`:

| | |
|---|---|
| Gates PASS | **5 of 27** |
| Gates FAIL | 0 |
| Gates UNVERIFIABLE | 22 — of which 16 are `NOT_YET_EXECUTABLE`, 3 `PARTIALLY_EXECUTABLE` |
| Assertions executed | **50 of 127** |
| Tests | 1317 passing, plus 126 driven through the chat client |

Zero failures is not the same as done. **Fewer than half the contract's
assertions have ever been executed**, and 16 gates cannot run yet because what
they gate does not exist. The honest summary is that the kernel, the model
policy, the gate machinery, the owner surface and the chat surface work, and
most of the contract remains unverified.

### Known defects in the chat path

- **Images are silently dropped.** `ChatMessage.text()` keeps only text parts, so
  an attached image vanishes before dispatch with no error. Image OCR cannot
  work until this is fixed, whatever model is seated.
- **Tool calls are impossible.** The request model ignores a client's `tools`
  array and work units are built with `tools=()`. Nothing in the UI's function
  calling can reach a model.
- **No token accounting.** No `usage` block is returned, so the client shows no
  token counts or cost.
- **Streaming is not incremental.** One chunk carries the whole reply, because
  provenance hashes the complete output. A 130-second research turn shows nothing
  until it all lands.

### Not built

- **§15 retrieval planes.** FINDING-007 delivered citation *enforcement* without
  retrieval. Note that Open WebUI's own file retrieval is a client feature and is
  not the contract's retrieval plane.
- **Judge calibration.** `minimum_agreement_to_gate` is deliberately `null`, so
  every judge stays advisory and deterministic oracles carry the gates.
- **GATE-D3-25** needs a real green PR merged by CI.

---

## 7. Modes other harnesses have that this one does not

For the record, since these get asked for by name. None of these exist here:

| Mode | What it usually means | Would it fit? |
|---|---|---|
| **shadow** | Run a change against production traffic without serving it | Fits the mutation/holdout machinery well |
| **canary** | Ship to a fraction, watch, roll back | Needs deploy + rollback the contract does not yet cover |
| **design** | Produce an architecture before decomposition | Sits naturally between research and plan |
| **code** | Direct file editing, no work-unit ceremony | Conflicts with §21.2 — the ceremony *is* the gate |
| **deep research** | Multi-hop research with a synthesis pass | Fits `research` + the unbuilt §15 planes |
| **edit** | Targeted edits to an existing artifact | Would need lease semantics |
| **dangerous** | Reduced guardrails, explicit consent | Directly contradicts the contract's authority limits |

Adding one is small — a name, role, system prompt, token budget and optional
graph in `src/orchestration/modes.py` — but **the role assignment is the real
decision**, because it determines separation, gateway and which gates apply. A
"code" or "dangerous" mode in particular cannot be added without an owner
decision, since both weaken §21.2 by design.

---

## 8. Quick reference

```bash
# environment (tests fail without it)
PY=/home/yoav/efah/.venv/bin/python
cd /home/yoav/efah/efah-harness
set -a && . ~/.efah/env && set +a

$PY -m pytest tests/ -q                                   # full suite
$PY -m pytest tests/integration/test_openwebui_e2e.py -q  # chat client, free
$PY -m pytest tests/integration/test_openwebui_e2e.py -q --run-live   # billed
$PY tools/check_assertion_hashes.py
PYTHONPATH=src $PY -m evaluation.gate_runner --json evidence/gate-run-summary.json
PYTHONPATH=src $PY tools/bench_chat_facade.py             # live façade sample
PYTHONPATH=src $PY tools/fuzz_generation_config.py <model>
PYTHONPATH=src $PY tools/requalify_model.py <model> -n 5

systemctl --user status efah-owner-surface efah-instruction-consumer
docker logs -f efah-openwebui
```

**Never record a model as failing without running `fuzz_generation_config.py`
first.** DEC-008: a failure is a configuration finding, not a verdict. One
success proves the model can do the task; one failure proves nothing.
