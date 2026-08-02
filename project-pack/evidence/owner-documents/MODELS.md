# ckff LiteLLM Proxy — Model Inventory & Benchmark Log

Cross-vendor frontier model bench fronting **ckff** through one LiteLLM proxy.
Benchmarked 2026-08-01. Every figure below is measured, not quoted.

- **Endpoint:** `https://litellm-production-8656.up.railway.app`
- **Auth:** per-agent virtual key (`sk-…`) via `/key/generate`, revocable via `/key/delete`
- **Upstream:** `https://ckff.dev/v1` (+ 2 identical mirrors)
- **Deployed:** 35 aliases · 41 routing entries · 8 key groups · **32/33 chat verified live**

> **Scope rule:** `config.yaml` is generated *only* from `ckff-*-model` vars in `.env`.
> No model name is hardcoded in the generator, so an unsolicited (and possibly
> expensive) route cannot be introduced by editing code. If it is not in `.env`,
> it does not deploy.
>
> No key values appear in this document — only env-var names.

---

# 1. What we tried, and what it concluded

## 1.1 Cost — billing follows the *channel group*, not the list price

The pricing table and the actual bill disagree, sometimes by orders of magnitude.
From account telemetry (`/api/log/self`, 1,000 rows):

| Model | Channel group | Calls | Tokens | Actual spend | Effective |
|---|---|---|---|---|---|
| `claude-opus-5` | `kiro-pro (没有缓存)` | 56 | 73 | **$0.0001** | ~free |
| `gpt-5.6-luna` | `codex-plus-cc` | 11 | 889 | $0.0002 | ~free |
| `grok-4.5` | `grok` | 88 | 972,541 | $0.054 | $0.056/M |
| `gpt-5.6-luna` | `codex-pro` | 14 | 62,519 | $0.0053 | $0.085/M |
| `minimax-m3` | `按量3` | 175 | 424,114 | $0.049 | $0.115/M |
| `[官4][次] glm-5.2` | `default` | 2 | 294 | **$0.16** | **$0.08/call** |

**Conclusion.** `claude-opus-5` lists at $75/$75 per M but billed **$0.0001 across 56
calls** on the `kiro-pro` channel. Conversely `glm-5.2` on `default` is the single most
expensive thing measured, at $0.08/call. **Never rank cost by the pricing table — rank
by observed spend per channel.**

## 1.2 Cost — flat-rate channels beat per-token for eval workloads

`[aws]*` and `[grok]` routes bill a **fixed fee per call**, independent of token count:

| Route | Billing | Same model, per-token route |
|---|---|---|
| `[grok] grok-4.5` | **FLAT $0.004/call** | `grok-4.5` @ $2.00/$6.00 per M |
| `[aws]glm-5` | FLAT $0.008/call | — |
| `[aws]kimi-k2-thinking` | FLAT $0.008/call | — |
| `[aws]minimax-m2.5` | FLAT $0.008/call | — |
| `[aws]deepseek-v3.2` | FLAT $0.008/call | — |
| `[官4][次] glm-5.2` | FLAT $0.080/call | — (20× the `[aws]` rate) |

**Conclusion.** On a 50k-token eval prompt, `[grok] grok-4.5` costs $0.004 while
`grok-4.5` costs ~$0.10 — the *same model*. For long-prompt eval work, prefer flat-rate
channels. For short/chatty traffic the per-token routes win. `[官4]` is never worth it.

## 1.3 Concurrency — spreading load across models does **not** help

Hypothesis tested: *hammering one model fails more than spreading across many.*
Controlled experiment — identical concurrency (N=10) and identical total requests in
every condition, only the spread varied, order reversed on the second repetition to
cancel time-of-day bias. Tool calling exercised on every request.

| Condition | Success | Tool calls | Median |
|---|---|---|---|
| A — one model, one key (`[aws]glm-5` ×10) | **20/20** | 20/20 | 1.3s |
| B — ten models, one key | **20/20** | 20/20 | 1.5s |
| C — one model, three keys | **20/20** | 20/20 | 3.0s |
| D — ten models, mixed keys | **20/20** | 20/20 | 1.9s |

**Conclusion — hypothesis not supported.** ckff enforces **100 requests per _minute_,
account-wide**. It is a *rate* limit, not a per-model *concurrency* limit, and it counts
total requests regardless of which model they hit. Diversifying models does not evade it;
only slowing down does. Sustained hammering of one model does exhaust the shared budget
faster, which is likely what made it *look* model-specific.

**Limitation, stated plainly:** tested at N=10. A per-channel concurrency ceiling above
that cannot be ruled out — pushing higher would hit the 100/min cap and confound the result.

## 1.4 Reliability — most "flaky" models were our own rate limiting

An early benchmark ran 14-way parallel and reported 0/3 or 1/3 for eleven models. Those
were `429 global rate limit exceeded (100 requests per minute)` — self-inflicted. Re-run
paced at ~40 req/min, **every one returned 3/3**:

`glm-4.7` · `kimi-k2.7-code` · `kimi-k2.5` · `qwen-3.6-max` · `qwen3.6-plus` ·
`qwen3.7-flash` · `minimax-m3` · `mimo-v2.5-pro` · `step-3.7-flash` · `glm-5v-turbo` ·
`kat-coder-pro-v2.5`

**Conclusion.** Any eval harness must throttle **globally**. Un-throttled parallelism
produces false negatives that are indistinguishable from genuine model failure.

## 1.5 Streaming vs non-streaming — no capability gap

Initially it appeared non-streaming failed where streaming worked. After pacing,
`nonstream=OK` on every model except genuinely-down channels. The apparent gap was the
rate cap: streaming requests happened to spread out in time.

**Conclusion.** Still prefer streaming for agents (better TTFB, avoids proxy timeouts on
long generations) — but not because non-streaming is broken.

## 1.6 Latency — variance matters more than the median

| Model | Median | Worst observed |
|---|---|---|
| `gpt-5.6-sol` | 4.7s | **119.4s** |
| `gpt-5.6-terra` | 1.4s | 62.3s |
| `gpt-5.6-luna` | 1.7s | 7.9s |
| `claude-opus-4-7` | 1.3s | 47.5s (under load) |
| `[aws]deepseek-v3.2` | 0.7s | 4.8s |

**Conclusion.** Medians hide the problem. `gpt-5.6-sol` is unusable for interactive work
despite a 4.7s median. `gpt-5.6-luna` has the tightest distribution of the codex family
and is the best interactive default. Claude models are fast when idle but degrade sharply
under concurrent load.

## 1.7 The CJK header bug — a real LiteLLM defect, now patched

LiteLLM copies the upstream model id into a Starlette **response header**. Headers encode
as latin-1, so any CJK id raises `UnicodeEncodeError` and returns 500 — **after** the
upstream call has already succeeded and been billed. You pay and get an error.

It needed patching in **two** places (the first fix only covered non-streaming):

| Path | Symptom |
|---|---|
| `starlette/datastructures.py:584` `MutableHeaders.__setitem__` | non-streaming 500 |
| `starlette/responses.py` `Response.init_headers` → `v.encode("latin-1")` | **streaming** 500 |

Fixed via `sitecustomize.py` + `ENV PYTHONPATH=/app/patches`. Python auto-imports
`sitecustomize` at startup, before LiteLLM builds any response. This unlocked 5 routes
that were previously impossible: `glm-5.2`, `deepseek-v4-pro`, `deepseek-v4-flash`,
`gemini-3.5-flash-search`, `gemini-3.1-pro-preview-search`.

## 1.8 Anthropic `/v1/messages` — upstream-limited, not fixable by config

Only ckff's **codex** and **gcli** channels implement it. Everything else returns
`not implemented / convert_request_failed`, regardless of streaming or tools. Verified
this is not a naming artefact by pointing a neutral `opus-5` alias at the identical
upstream — it failed the same way.

**Usable for Claude Code:** `gpt-5.6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`,
`gemini-3.5-flash`, `gemini-3.1-pro-preview`. Nothing else.

---

# 2. Tool-calling reliability (3 attempts each, paced)

## Reliable — 3/3

| Model | Key group | Median |
|---|---|---|
| `claude-sonnet-4-6` | kiro | 1.5s |
| `[aws]kimi-k2-thinking` | default | 1.6s |
| `claude-opus-4-7` | kiro | 1.7s |
| `[aws]glm-5` | default | 1.8s |
| `claude-sonnet-5` | kiro | 1.9s |
| `gemini-3.5-flash` | gemini_cli | 2.0s |
| `[ds2] deepseek-v4-flash` | default | 2.0s |
| `gpt-5.6-luna` | codex-pro | 2.1s |
| `claude-opus-4-8` | kiro | 2.1s |
| `gpt-5.6-terra` | codex-pro | 2.4s |
| `gpt-5.5` | codex-pro | 2.4s |
| `qwen3.7-flash` | kimi | 2.5s |
| `[aws]minimax-m2.5` | default | 3.0s |
| `[ds2] deepseek-v4-pro` | default | 3.0s |
| `gemini-3.1-flash-lite-preview` | gemini_cli | 3.0s |
| `glm-5-turbo` | kimi | 3.4s |
| `[grok] grok-4.5` | default | 4.0s |
| `grok-4.5` | grok | 4.2s |
| `[aws]deepseek-v3.2` | default | 4.8s |
| `qwen-3.6-max` | kimi | 5.1s |
| `claude-haiku-4-5-20251001` | kiro | 9.6s |

## CORRECTION (re-audited) — every model supports tool calling

The table above understated support. An escalating re-audit of **50 models**
found **45/45 reachable models emit tool calls, and zero do not**.

The earlier "no tool calling" and "intermittent" verdicts were **measurement
artefacts**: those runs used `max_tokens` of 16/32/64. Reasoning models emit
`reasoning_content` *before* `tool_calls`, so the response truncated before the
call was ever produced.

Protocol used — a model is only declared unsupported after failing all four:

| Stage | Request |
|---|---|
| S1 | `max_tokens=512`, streaming |
| S2 | `max_tokens=512`, non-streaming |
| S3 | `max_tokens=1024`, non-streaming, `tool_choice="auto"` |
| S4 | `max_tokens=1024`, non-streaming, `tool_choice="required"`, imperative prompt |

**Previously mis-reported — all pass at S1:** `kimi-k2.7-code`, `mimo-v2.5-pro`,
`step-3.7-flash`, `deepseek-r1`, `claude-sonnet-4-6`, `claude-haiku-4-5`,
`gemini-2.5-flash`, `gpt-5-mini`, `kimi-k2.5`, `glm-5v-turbo`,
`kat-coder-pro-v2.5`, `gemini-3.1-pro-preview`.

**Genuine quirk, one model:** `minimax-m3` emits tool calls only at S2
(**non-streaming**). Streaming clients will see none.

**Rule for any harness: never test or use tool calling with `max_tokens` below
256.** Doing so produces false negatives that are indistinguishable from a model
genuinely lacking support.

## Currently unreachable (channel outage, not capability)

`[aws]deepseek-v3.2` · `[aws]glm-5` · `[aws]glm-4.7` · `[aws]kimi-k2-thinking` ·
`[aws]minimax-m2.5` — all return `503 No available channel`. This is the entire
flat-rate $0.008 lane; it worked earlier the same day.

---

# 3. Deployed model list by key group

### `codex` — `CKFF_CODEX_PRO_KEY` / `_PLUS_` / `_CC_`
| Alias | Price | Median | Tools | Anthropic |
|---|---|---|---|---|
| `gpt-5.6-luna` | $1.00/$6.00 per M | 2.1s | Y | yes |
| `gpt-5.6-terra` | $2.50/$15.00 per M | 4.3s | Y | yes |
| `gpt-5.5` | $5.00/$30.00 per M | 13.0s | Y | yes |
| `gpt-5.6-sol` | $5.00/$30.00 per M | 4.7s (max 119s) | Y | yes |

`luna` is pinned to `codex-pro` alone — it timed out >150s on `plus` and `CC`.
`sol`/`terra`/`gpt-5.5` pool across all three keys.

### `kiro` — `CKFF_KIRO_KEY`
| Alias | List price | Effective | Median | Tools |
|---|---|---|---|---|
| `claude-opus-4-7` | $5/$25 per M | — | 2.3s | Y |
| `claude-opus-4-8` | $5/$25 per M | — | 3.5s | Y |
| `claude-sonnet-5` | $3/$15 per M | — | 2.3s | Y |
| `claude-sonnet-4-6` | $3/$15 per M | — | 2.1s | Y |
| `claude-opus-5` | $75/$75 per M | **~$0 observed** | 2.7s | Y |

### `default` — `CKFF_DEFAULT_KEY`
| Alias | Upstream id | Price | Median | Tools |
|---|---|---|---|---|
| `[grok] grok-4.5` | same | FLAT $0.004/call | 3.4s | Y |
| `[aws]glm-5` | same | FLAT $0.008/call | 8.8s | Y |
| `[aws]glm-4.7` | same | FLAT $0.008/call | 1.0s | Y |
| `[aws]kimi-k2-thinking` | same | FLAT $0.008/call | 1.1s | Y |
| `[aws]minimax-m2.5` | same | FLAT $0.008/call | 1.3s | Y |
| `[aws]deepseek-v3.2` | same | FLAT $0.008/call | 1.9s | Y |
| `[ds2] deepseek-v4-pro` | same | $0.43/$0.87 per M | 2.0s | Y |
| `[ds2] deepseek-v4-flash` | same | $0.14/$0.28 per M | 2.1s | Y |
| `glm-5.2` | `[官4][次] glm-5.2` | FLAT $0.080/call | 15.9s | Y |
| `deepseek-v4-pro` | `[三方4][次] deepseek-v4-pro [不补]` | quota-limited | 5.2s | Y |
| `deepseek-v4-flash` | `[三方4][次] deepseek-v4-flash [不补]` | — | 6.5s | Y |
| `gemini-3.5-flash-search` | `[gcli] … [不补]` | — | 2.4s | Y |
| `gemini-3.1-pro-preview-search` | `[gcli] … [不补]` | — | intermittent¹ | Y |

¹ upstream returns `当前无可用凭证` ("no credentials available") ~1 in 3; retry succeeds.

### `kimi` — `CKFF_KIMI_KEY` (`按量3`)
| Alias | Price | Median | Tools |
|---|---|---|---|
| `qwen3.7-flash` | $0.10/$0.20 per M | 2.8s | Y |
| `minimax-m3` | $0.30/$1.20 per M | 2.2s | intermittent |
| `qwen3.6-plus` | $0.50/$3.00 per M | 5.3s | Y |
| `kimi-k2.7-code` | $0.74/$3.50 per M | 3.1s | **no** |
| `glm-5-turbo` | $1.20/$4.00 per M | 3.8s | Y |
| `qwen-3.6-max` | $1.30/$7.80 per M | 6.4s | Y |
| `mimo-v2.5-pro` | $0.43/$0.87 per M | 20.0s | **no** |

### `gemini_cli` · `grok` · `image`
| Alias | Key group | Price | Median | Tools |
|---|---|---|---|---|
| `gemini-3.5-flash` | gemini_cli | $1.50/$9.00 per M | 1.2s | Y |
| `gemini-3.1-flash-lite-preview` | gemini_cli | $0.25/$1.50 per M | 1.2s | Y |
| `gemini-3.1-pro-preview` | gemini_cli | $2.00/$12.00 per M | 3.4s | intermittent |
| `grok-4.5` | grok | $2.00/$6.00 per M | 4.1s | Y |
| `gemini-3.1-flash-lite-image` | imagegen | FLAT $0.02/call | — | image |
| `grok-imagine-image` | imagegen | FLAT $0.02/call | — | image |

---

# 4. Recommended eval-lab set

**Frontier, one per vendor — all tool-reliable:**
`gpt-5.6-terra` (OpenAI) · `claude-opus-4-7` (Anthropic) · `gemini-3.5-flash` (Google) ·
`[grok] grok-4.5` (xAI) · `[ds2] deepseek-v4-pro` (DeepSeek) · `[aws]glm-5` (Zhipu) ·
`[aws]kimi-k2-thinking` (Moonshot) · `qwen-3.6-max` (Qwen) · `[aws]minimax-m2.5` (MiniMax)

**High-volume workhorses:** `[aws]deepseek-v3.2` (0.7s, flat) · `qwen3.7-flash`
($0.10/$0.20) · `[ds2] deepseek-v4-flash` · `gemini-3.1-flash-lite-preview` · `[aws]glm-4.7`

**Reasoning-effort variants** — `reasoning_effort` is a *parameter*, not a model id:

| Model | `xhigh` | `high` |
|---|---|---|
| `gpt-5.5` | OK 3.7s | OK 2.4s |
| `gpt-5.6-terra` | OK 1.5s | — |
| `gpt-5.4` | 503 | 503 |

**Currently down:** `gpt-5.4` and `gpt-5.4-mini` — `503 Service temporarily unavailable`,
0/3 across two independent runs. The `gpt-5.4` codex family is degraded; `5.5`/`5.6` fine.

---

# 5. Required configuration

```yaml
litellm_settings:
  drop_params: true
  num_retries: 5
  request_timeout: 90        # above slowest healthy call, below observed hangs
  telemetry: false
  # No cross-model fallbacks: a request returns its model or errors.

router_settings:
  routing_strategy: latency-based-routing
  num_retries: 5
  retry_after: 2
  allowed_fails: 8           # loose: luna has a single key
  cooldown_time: 20
  max_parallel_requests: 8   # ckff caps at 100 req/min ACCOUNT-WIDE
```

Plus `sitecustomize.py` + `ENV PYTHONPATH=/app/patches` in the Dockerfile (section 1.7).

**Host redundancy** — all three serve identical models with the same keys:
`https://ckff.dev/v1` · `https://68886868.xyz/v1` · `https://api2.68886868.xyz/v1`
