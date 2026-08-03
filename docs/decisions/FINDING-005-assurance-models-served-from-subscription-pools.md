# FINDING-005 — assurance models are served from resold subscription pools

**Raised:** 2026-08-02 · **Class:** `OWNER_RISK_ACCEPTANCE` — owner decision required
**Status:** OPEN. Nothing changed. **This supersedes FINDING-003 in priority.**
**Evidence:** the owner's own ckff account request log, retrieved 2026-08-02 via
`GET /api/log/self`. Not inference.

## Measured

| Role | Model | Upstream group | Channel |
|---|---|---|---|
| **sealed_holdout_author** | claude-opus-4-8 | **`kiro-pro`** | 234 |
| **visible_test_author** | claude-opus-4-7 | **`kiro-pro`** | 234 |
| **contract_compliance_auditor** | claude-sonnet-5 | **`kiro-pro`** | 234 |
| **mutant_author** | gemini-3.5-flash | **`gemini-cli`** | 248 |
| oracle_author | qwen-3.6-max | `按量3` | 139 |
| release_verifier | glm-5-turbo | `按量3` | 139 |
| adversarial_critic | grok-4.5 | `default` | 185 |
| judge | deepseek-v4-pro | `default` | 192 |
| implementer *(candidate)* | gpt-5.6-luna | `codex-pro` | 214 |

`kiro-pro` = AWS Kiro coding-agent accounts. `gemini-cli` = Gemini CLI quota.
Neither is an official API. The relay operator's own public notice board
(`/api/status`) documents the supply: *"Kiro discount accounts all dead, using
Pro accounts to fill in temporarily"* (2026-06-09), Claude Code Max resold as
API (2026-07-31), Antigravity IDE quota, Gemini CLI quota.

## The finding that matters most

**One channel serves several "different" models.**

- ch234 → `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`
- ch248 → `gemini-3.1-flash-lite-preview`, `gemini-3.1-pro-preview`, `gemini-3.5-flash`
- ch139 → six models: `glm-5-turbo`, `kimi-k2.7-code`, `mimo-v2.5-pro`, `minimax-m3`, `qwen-3.6-max`, `qwen3.7-flash`

`model-policy.yaml → role_incompatibilities` enforces `implementer ≠
sealed_holdout_author` **by alias and family**, and the harness checks it
mechanically. But the check is against a *label*. Three labels resolving to one
Kiro account pool means the separation is real in the control plane and possibly
fictional at the transport.

Contract §12.2 — *"same-family validation MUST be rejected where family bias is
material"* — is enforced on data that the transport does not guarantee.

## Why assurance specifically

A degraded implementer produces a worse candidate and the gates catch it. A
degraded *assurance* model does not error: it emits plausible tests that pass.
Assurance output is by construction the thing nothing downstream checks, so the
sealed-holdout guarantee inverts — instead of catching what the implementer
missed, it certifies it.

This is the same failure DEC-002 exists to prevent (*the recorded run is not the
run that happened*), one layer up, and with no `num_retries` field to hint at it.

## Official channels exist on this platform

`[官转1] qwen3-coder-next` runs on channel 1. **官转 = official relay.** So
official-API channels are available here; the assurance roles are simply not on
them.

Also observed: `[Kiro次] claude-opus-5-thinking [不补]` — Opus 5 via Kiro,
per-request, `不补` = no refund on failure. Independently, `claude-opus-5-thinking`
could not be corroborated as a real Anthropic model id: Anthropic exposes
extended thinking as a *parameter*, not a model. `model-policy.yaml`'s
`prohibited_models` note recommends this id as the safe frontier Anthropic route.

## Options

- **A — official credentials for the eval gateway only.** Nine gate-bearing
  roles, low volume. Candidate work stays on ckff, where retries and cheapness
  are the point and the gates catch quality. `environments.yaml` edit plus a
  key; the architecture already treats the gateway as swappable.
- **B — select ckff's `官转` (official-relay) channels explicitly** for
  gate-bearing roles, if the platform allows pinning a channel per request.
  Cheaper than A. Unverified whether it is possible; needs a probe.
- **C — keep the current transport and instrument detection.** Per-request
  model-echo and time-to-first-token assertions, hard-fail on empty/truncated
  generations, and a **private mutant corpus with known kill difficulty** that
  the assurance model must keep killing. Measures capability instead of trusting
  ids — but audits a transport that cannot be pinned.
- **D — accept and record.** Then the honest-debt entry must say the assurance
  path's model provenance is unverified, and the mutation kill rate is weaker
  evidence than it appears.

**Recommendation: A for the nine gate-bearing roles, C's private mutant corpus
regardless of which is chosen.** The corpus is the only check that catches both
substitution and the weak-oracle failure, and it measures the capability that
actually matters rather than a leaderboard proxy.

---

## Option B is eliminated — measured 2026-08-02

`autonomy-policy.yaml → question_policy.must_not_ask_about` includes
`anything_safely_measurable_by_probe`, and B was recorded as *unverified, needs
a probe*. It has now been probed rather than asked about.
`tools/probe_finding_005.py`, evidence in
`evidence/FINDING-005-transport-probe.json`. Two model requests, serial, under
the global throttle, zero client retries.

**B1 — pinning is real, and the pack already uses it.** ckff carries the channel
tag *inside the model name*: `[官转1] claude-sonnet-4-5`, `[Kiro] claude-opus-4-8`.
`model-policy.yaml` already routes `adversarial_critic` to `[grok] grok-4.5` and
`judge` to `[ds2] deepseek-v4-pro`. So the mechanism is not in question.

**B2 — the official channels do not carry the work.** 203 models, 23 channel
prefixes, and exactly **9 models on an official channel** (`官转1`, `官3`, `官4`):

```
[官转1] claude-sonnet-4     [官转1] claude-sonnet-4-5   [官转1] deepseek-3.2
[官转1] glm-5               [官转1] minimax-m2.5        [官转1] qwen3-coder-next
[官3] kimi-k3               [官4][次] glm-5.2           [官4][量] glm-5.2
```

**Official routes covering a gate-bearing role's configured model: 0 of 9.**
No official route to any Opus, none to `claude-sonnet-5`, and **no official
Google channel at all** — so `mutant_author` cannot be pinned under any
substitution. The best official Anthropic route is `claude-sonnet-4-5`, two
generations below the configured `claude-opus-4-8`.

**B3 — the eval gateway will not forward an unconfigured pinned name.**

```
POST /v1/chat/completions  model="[官转1] claude-sonnet-4"
  → 400  "Invalid model name passed in model=[官转1] claude-sonnet-4"
```

`litellm_eval` is DB-less by contract (`must_remain_dbless: true`), so its model
list is static config on the owner's Railway deployment. The builder cannot add
a route, and `redesign_permitted: false` forbids it from trying.

**Therefore B costs an owner gateway change — the same operational cost as A —
while also forcing a capability downgrade A does not.** B was the cheap option;
it is not cheap, so it is dominated by A and is withdrawn.

## The measurement reconfirmed, on the gateway path specifically

The same probe sent the configured `sealed_holdout_author` model through the
**eval** gateway and asked the upstream's own accounting who served it:

```
model=claude-opus-4-8 → 200, 4.13s
  upstream log: channel 234, group "kiro-pro (没有缓存)"
  request_id 202608021335245944979348268d9d6UXTV3A8s
```

FINDING-005's original measurement is reconfirmed today, and now bound to the
eval gateway rather than to the account in general. The gate-bearing path is
served from a resold Kiro subscription pool.

## The question that remains, narrowed

Options are now **A, C, or D**. A requires a credential and a gateway edit only
the owner can make (§29: *required credentials or service endpoints*). C and D
are buildable by the builder today.

Raised to the owner as blocker `FINDING-005-transport` through the control
surface — not as a second question round, which `question_policy` forbids.
Per §20.3 and `on_timeout: continue_unblocked_work_and_hold_blocked_tasks`, only
the generation of sealed holdout **content** is held. Everything else in DEC-006
— the identity, the store, the generator, the mutation gate, the private-corpus
machinery — is unblocked and proceeds, because C's corpus is recommended under
every option and A/C/D differ only in which credential the generator is handed.

## Relationship to FINDING-003

FINDING-003 (tier labels contradicting their models) is now **second-order**.
Choosing a higher-benchmark label does not help while three labels resolve to one
account pool. Resolve the transport first.
