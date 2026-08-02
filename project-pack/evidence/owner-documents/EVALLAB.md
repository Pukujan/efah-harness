# Cross-Vendor Frontier Model Eval Lab — Proposed Model Set

Benchmarked against ckff on 2026-08-01. Every model below was probed with
**3 streaming calls + 1 tool-call probe + 1 non-streaming probe**, paced to respect
ckff's rate cap. Nothing here is deployed yet — this is a proposal.

---

## Methodology and two corrections worth recording

1. **ckff enforces a global 100 req/min cap across the whole account.** An early
   14-way parallel benchmark tripped it and produced false `429`s that looked like
   model unreliability. Re-tested at ~40 req/min, **every** flagged model returned 3/3.
   *Any eval harness must throttle globally, not per-model.*

2. **Billing follows the channel group, not the list price.** Telemetry shows
   `claude-opus-5` on `kiro-pro` billed **$0.0001 across 56 calls** despite a $75/M
   list rate. Conversely `[官4][次] glm-5.2` on `default` billed **$0.08/call**.
   Judge cost by observed spend per group, not the pricing table.

3. **`[aws]*` and `[grok]` routes bill a FLAT per-call fee**, independent of tokens.
   For eval work with long prompts these are dramatically cheaper than per-token routes.

---

## Tier A — Frontier flagships, one per vendor

All 3/3 reliable, streaming + tool calling verified.

| Vendor | Model id | Key group | Price | Median | Tools |
|---|---|---|---|---|---|
| OpenAI | `gpt-5.6-terra` | codex-pro/plus/CC | $2.50 / $15.00 per M | 1.4s | Y |
| OpenAI | `gpt-5.5` | codex-pro/plus/CC | $5.00 / $30.00 per M | 1.7s | Y |
| Anthropic | `claude-opus-4-7` | kiro | $5.00 / $25.00 per M | **1.3s** | Y |
| Anthropic | `claude-opus-4-8` | kiro (`cc-max`) | $5.00 / $25.00 per M | 2.7s | Y |
| Google | `gemini-3.1-pro-preview` | gemini_cli | $2.00 / $12.00 per M | 4.3s | Y |
| xAI | `[grok] grok-4.5` | default | **FLAT $0.004/call** | 3.3s | Y |
| DeepSeek | `[ds2] deepseek-v4-pro` | default | $0.43 / $0.87 per M | 1.9s | Y |
| Zhipu | `[aws]glm-5` | default | **FLAT $0.008/call** | 1.8s | Y |
| Moonshot | `[aws]kimi-k2-thinking` | default | **FLAT $0.008/call** | 2.0s | Y |
| Qwen | `qwen-3.6-max` | kimi (`按量3`) | $1.30 / $7.80 per M | 5.2s | Y |
| MiniMax | `[aws]minimax-m2.5` | default | **FLAT $0.008/call** | 1.9s | Y |

**Best value in the whole table:** `[grok] grok-4.5` at $0.004/call flat — the same
model as `grok-4.5` ($2/$6 per M) on a different channel. On a 50k-token eval prompt
that is roughly a 25,000× cost difference.

## Tier B — High-volume workhorses

| Model | Key group | Price | Median | Tools |
|---|---|---|---|---|
| `[aws]deepseek-v3.2` | default | FLAT $0.008/call | **0.7s** (fastest tested) | Y |
| `qwen3.7-flash` | kimi | $0.10 / $0.20 per M | 3.0s | Y |
| `[ds2] deepseek-v4-flash` | default | $0.14 / $0.28 per M | 1.9s | Y |
| `gemini-3.1-flash-lite-preview` | gemini_cli | $0.25 / $1.50 per M | 3.0s | Y |
| `minimax-m3` | kimi | $0.30 / $1.20 per M | 2.7s | Y |
| `[aws]glm-4.7` | default | FLAT $0.008/call | 0.9s | Y |
| `gpt-5.6-luna` | codex-pro | $1.00 / $6.00 per M | 1.9s | Y |

## Tier C — Reasoning-effort variants (tested)

`reasoning_effort` is a request parameter, not a separate model id.

| Model | `xhigh` | `high` | Note |
|---|---|---|---|
| `gpt-5.5` | **OK 3.7s** | OK 2.4s | both work |
| `gpt-5.6-terra` | **OK 1.5s** | — | works |
| `gpt-5.4` | 503 | 503 | channel down, see below |

Send as `{"model":"gpt-5.5","reasoning_effort":"xhigh"}`. LiteLLM passes it through;
`drop_params: true` will silently strip it for models that reject it, so verify per model.

---

## Excluded, with reasons

| Model | Why excluded |
|---|---|
| `gpt-5.4`, `gpt-5.4-openai-compact` | **503 Service temporarily unavailable** — channel outage, 0/3 across two runs |
| `claude-opus-5` | $75/$75 per M list. Billed ~free on `kiro-pro`, but the exposure is unbounded if that channel changes |
| `[官4][次] glm-5.2` | **$0.08/call flat** — 20× the `[aws]` rate for a comparable model |
| `gpt-5.6-sol` | $5/$30 per M and latency swings 4.7s–120s |
| `claude-haiku-4-5` | 6.8s median, no tool calling |
| `deepseek-r1`, `gemini-2.5-flash`, `gpt-5-mini` | no tool calling |
| `kimi-k2.7-code`, `kimi-k2.5`, `mimo-v2.5-pro`, `step-3.7-flash`, `glm-5v-turbo`, `kat-coder-pro-v2.5` | tool calling **intermittent** — passed on one run, failed on another. Unsuitable for agentic eval; fine for text-only |

---

## Required configuration changes

### 1. CJK header patch (already deployed)
`sitecustomize.py` + `ENV PYTHONPATH=/app/patches` in the Dockerfile. Without it every
`[官4]`/`[三方4]`/`[不补]` route 500s *after* the upstream call succeeds and bills.
This unlocked `glm-5.2` and both `-search` models.

### 2. Global throttle
ckff caps at **100 req/min account-wide**. Set in `config.yaml`:
```yaml
router_settings:
  max_parallel_requests: 8
```
Without this a parallel eval sweep self-inflicts 429s that look like model failures.

### 3. Host redundancy
All three hosts serve identical models with the same keys:
`https://ckff.dev/v1` · `https://68886868.xyz/v1` · `https://api2.68886868.xyz/v1`
Add the alternates as extra pool members for automatic failover.

### 4. Anthropic-route restriction
`/v1/messages` works only on ckff's **codex** and **gcli** channels. Every other model
returns `not implemented`. Anything driving Claude Code must use a codex or gemini model.

---

## Suggested `.env` additions

Paste these to make the generator pick the set up — `config.yaml` is generated
from `.env` only, so nothing deploys until these exist:

```bash
# Tier A frontier flagships
ckff-cortex-default-frontier-model=[grok] grok-4.5,[aws]glm-5,[aws]kimi-k2-thinking,[aws]minimax-m2.5,[ds2] deepseek-v4-pro
ckff-kiro-pro-model=claude-opus-4-7,claude-opus-4-8
ckff-cortex-codex-models=gpt-5.6-luna,gpt-5.6-terra,gpt-5.5
ckff-cortex-gemini-cli-model=gemini-3.1-pro-preview,gemini-3.1-flash-lite-preview
ckff_cortex_kimi_token_model=qwen-3.6-max,qwen3.7-flash,minimax-m3

# Tier B workhorses
ckff-cortex-default-cheap-model=[aws]deepseek-v3.2,[aws]glm-4.7,[ds2] deepseek-v4-flash
```
