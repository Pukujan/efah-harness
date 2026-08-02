# LiteLLM Configuration Guide — read this before touching the proxy

Written for agents and humans working on any project that talks to the ckff
gateways. It exists because several expensive mistakes were made building this,
and every one of them is easy to repeat.

## What LiteLLM actually is

Not just an API shim. It is a **gateway with a policy layer**:

| Capability | Why it matters to you |
|---|---|
| Unified OpenAI **and** Anthropic surface over ~100 providers | one endpoint, any client SDK |
| Virtual keys, per-key budgets and model allowlists | give each project/agent a revocable key |
| Routing + load balancing across several upstream keys | throughput, failover |
| **Retries, fallbacks, cooldowns** | helpful in prod, **fatal for evaluation** |
| Spend tracking, logging, caching | telemetry per key |

That policy layer is the thing to think about. It will silently retry, re-route,
and rewrite requests unless told not to. A coding agent wants that. An eval
harness must not have it. Same binary, different config — which is why there are
two deployments.

## The two gateways

| | Production | Eval |
|---|---|---|
| Service | `litellm` | `litellm-eval` |
| URL | `litellm-production-8656.up.railway.app` | `litellm-eval-production.up.railway.app` |
| Retries | 5 + 5 | **0 + 0** |
| Routes per alias | up to 3 (pooled) | **exactly 1** |
| `drop_params` | `true` | **`false`** |
| Cooldowns | on | off |
| Database | Postgres (virtual keys) | none |

**Never collect evaluation evidence through the production gateway.** It retries,
pools, and silently strips params.

---

# The five traps

## Trap 1 — `max_tokens` too small makes tool calling look broken

**This one produced eleven false negatives.** Reasoning models emit
`reasoning_content` *before* `tool_calls`. With `max_tokens` of 16–64 the
response truncates before the tool call is ever emitted, and the model looks
like it has no tool support.

A full escalating re-audit of 50 models found **45/45 reachable models support
tool calling and zero do not**. Earlier verdicts claiming otherwise were wrong.

```python
# WRONG - will report "no tool calling" on any reasoning model
{"model": m, "max_tokens": 32, "tools": TOOLS}

# RIGHT
{"model": m, "max_tokens": 512, "tools": TOOLS}
```

**Rule: never test or use tool calling with `max_tokens` below 256.**
Models needing the headroom include `kimi-k2.7-code`, `mimo-v2.5-pro`,
`step-3.7-flash`, `deepseek-r1`, `qwen*`, `glm*`, `kat-coder-pro-v2.5`.

## Trap 2 — the account rate cap looks like model failure

ckff enforces **100 requests per minute account-wide**. It is a *rate* limit, not
a per-model concurrency limit, and it counts every request regardless of which
model it hits.

A 14-way parallel benchmark tripped it and reported 0/3 or 1/3 for eleven models.
Re-run paced at ~40 req/min, **every one returned 3/3**.

Tested and disproven: spreading load across many models does **not** help.
Four conditions at N=10 (one model/one key, ten models/one key, one model/three
keys, ten models/mixed keys) all returned 20/20. Only slowing down helps.

**Rule: throttle globally, roughly 1 request per 0.9s. Never fan out unpaced.**

## Trap 3 — `drop_params: true` silently rewrites your request

LiteLLM strips any parameter the upstream rejects, without telling the caller.
A project sets `reasoning_effort: "xhigh"`, LiteLLM drops it, the model answers
normally, and the experiment records a setting that was never applied.

Production keeps `drop_params: true` (convenience). **Eval sets it `false`** so an
unsupported param is a loud 400 instead of a silent lie.

Known rejection: **Anthropic models 400 on `reasoning_effort`**
(`claude-opus-4-7`, `claude-opus-5`, `claude-sonnet-5`). Use
`claude-opus-5-thinking`, which accepts it.

## Trap 4 — pooling is a hidden retry

An alias backed by several keys silently re-rolls a failed call onto another key.
It never appears in `num_retries`. In production `gpt-5.5`, `gpt-5.6-sol` and
`gpt-5.6-terra` each had **3** routes. The eval config pins every alias to one.

**Rule: for any measurement, one alias = one upstream route.**

## Trap 5 — CJK model ids crash the response, after you have paid

LiteLLM writes the upstream model id into a Starlette response header; headers
are latin-1 only. Any id containing CJK (`[官4]`, `[三方4]`, `[不补]`) raises
`UnicodeEncodeError` and returns 500 — **after** the upstream call succeeded and
billed. You pay and get an error.

It needs patching in **two** places; fixing only the first leaves streaming broken:

| Path | Breaks |
|---|---|
| `starlette/datastructures.py` `MutableHeaders.__setitem__` | non-streaming |
| `starlette/responses.py` `Response.init_headers` | **streaming** |

Both are patched in `sitecustomize.py`, loaded via `ENV PYTHONPATH=/app/patches`.
**Do not remove it** — roughly a third of the catalogue has CJK ids.

---

# Per-project configuration recipes

## Coding agent / interactive assistant
```yaml
litellm_settings: {drop_params: true, num_retries: 3, request_timeout: 120}
router_settings:  {routing_strategy: latency-based-routing, num_retries: 3,
                   allowed_fails: 8, cooldown_time: 20, max_parallel_requests: 8}
```
Pool aliases across keys. Retries and failover are what you want.

## Evaluation / benchmarking
```yaml
litellm_settings: {drop_params: false, num_retries: 0, request_timeout: 120}
router_settings:  {routing_strategy: simple-shuffle, num_retries: 0,
                   cooldown_time: 0, disable_cooldowns: true}
```
One route per alias, no fallbacks, no `max_parallel_requests` (a 429 is evidence,
not something to hide). Client SDKs must also set `max_retries=0` — OpenAI and
Anthropic both default to **2**. Validate with
`litellm-eval/validate_eval_config.py` before every campaign.

## Batch / high-volume
Use flat-per-call routes; cost is independent of prompt length. Still throttle to
<100 req/min. Prefer non-streaming for simpler accounting.

## Client settings that apply everywhere
```python
OpenAI(base_url=URL+"/v1", api_key=KEY, max_retries=0, timeout=120)
Anthropic(base_url=URL,    auth_token=KEY, max_retries=0, timeout=120)
```
`urllib3.Retry` and most `HTTPAdapter` presets retry by default — check shared
sessions.

---

# Model quirks (measured)

| Model | Quirk |
|---|---|
| `minimax-m3` | tool calls only surface **non-streaming** |
| `claude-opus-4-7` / `opus-5` / `sonnet-5` | 400 on `reasoning_effort` |
| `claude-opus-5-thinking` | accepts `reasoning_effort` |
| `[aws]*` (5 routes) | currently **503 No available channel** |
| `[不稳定渠道] *` | channel is literally named "unstable"; 36–66s latencies seen |
| `gpt-5.4`, `gpt-5.4-mini` | 503, channel outage |
| `gpt-5.6-sol` | latency 4.7s–120s; unusable interactively |
| `deepseek-v4-pro` via `[三方4]` | 429 daily quota; `[ds2]` route is fine |

Universally supported across 21 models tested: system prompt, `temperature`,
JSON mode, `stop`, `seed`, multi-turn, 8k context.

# Anthropic `/v1/messages`

Only ckff's **codex** and **gcli** channels implement it. Everything else returns
`not implemented`. Verified not to be a naming artefact. Anything driving Claude
Code must use `gpt-5.6-*` or `gemini-*`.

# Changing the model list

`config.yaml` is **generated** from `ckff-*-model` vars in `.env`
(`scratchpad/build2.py` for production, `buildeval.py` for eval). No model name is
hardcoded, so an unsolicited or costly route cannot be introduced by editing code.
Add models to `.env`, regenerate, redeploy. Never hand-edit `config.yaml`.

# Cost

Rank by **observed spend per channel**, never by the pricing table. `claude-opus-5`
lists at $75/$75 per M but billed $0.0001 across 56 calls on `kiro-pro`;
`[官4] glm-5.2` on `default` billed $0.08/call, the most expensive thing measured.

## Budget lanes

Reference call = ~2k input + 500 output, typical for eval. Flat-rate routes are
billed per call regardless of size, so they win at any realistic prompt length.

### Lane $ — flat rate, cheapest, all tool-verified
| Model | Cost/call | Latency |
|---|---|---|
| `qwen3-coder-next` | **$0.002** | 1.3s |
| `[grok] grok-4.5` | $0.004 | 3.5s |
| `gemini-3.6-flash` | $0.005 | 19–66s ⚠ unstable channel |
| `gemini-3.5-flash-high` | $0.006 | 1.8s |
| `[aws]glm-5` / `glm-4.7` / `deepseek-v3.2` / `kimi-k2-thinking` / `minimax-m2.5` | $0.008 | **503 — channel down** |

### Lane $$ — cheap per-token, for short prompts
| Model | Price per M | Ref. call |
|---|---|---|
| `qwen3.7-flash` | $0.10 / $0.20 | $0.0003 |
| `[ds2] deepseek-v4-flash` | $0.14 / $0.28 | $0.0004 |
| `minimax-m3` | $0.30 / $1.20 | $0.0012 |
| `mimo-v2.5-pro` | $0.43 / $0.87 | $0.0013 |
| `[ds2] deepseek-v4-pro` | $0.43 / $0.87 | $0.0013 |
| `kimi-k2.7-code` | $0.74 / $3.50 | $0.0033 |

### Lane $$$ — frontier, justified cost
| Model | Price | Ref. call |
|---|---|---|
| `claude-opus-5-thinking` | FLAT $0.048 | $0.048 |
| `gpt-5.6-luna` | $1.00 / $6.00 per M | $0.005 |
| `[三方4] glm-5.2` | FLAT $0.035 | $0.035 |
| `gpt-5.6-terra` | $2.50 / $15.00 per M | $0.013 |
| `claude-opus-4-7` | $5.00 / $25.00 per M | $0.023 |

## Avoid — strictly worse than an equivalent on another channel

| Avoid | Cost | Use instead | Saving |
|---|---|---|---|
| **`claude-opus-5`** | $75/$75 per M → **$0.188**/ref call | `claude-opus-5-thinking` @ $0.048 flat | **~4×** |
| `grok-4.5` (per-token) | $2/$6 → $0.007 | `[grok] grok-4.5` @ $0.004 flat | ~2×, more at length |
| `gemini-3.5-flash` | $1.50/$9 → $0.0075 | `gemini-3.5-flash-high` @ $0.006 flat | ~1.3×, more at length |
| `[官4] glm-5.2` | **$0.08/call** | `[三方4] glm-5.2` @ $0.035 | ~2.3× |
| `gpt-5.6-sol` | $5/$30 **and** 4.7–120s latency | `gpt-5.6-terra` or `luna` | 2–5× + far faster |
| `gpt-5.5` | $5/$30 → $0.025 | `gpt-5.6-terra` $0.013 / `luna` $0.005 | 2–5× |
| `gemini-3.1-pro-preview` | $2/$12 → $0.010 | `gemini-3.5-flash-high` @ $0.006 flat | ~1.7× |

`claude-opus-5` is the single biggest cost trap in the catalogue. It billed near
zero historically on `kiro-pro`, but that is channel behaviour that can change —
the list rate is 15× the next most expensive model. Prefer
`claude-opus-5-thinking`, which is the same generation, flat-rate, and accepts
`reasoning_effort`.

**Caveat on flat-rate:** below roughly 200 total tokens a per-token route is
cheaper. Flat rate wins for eval prompts, loses for chatty one-liners.

⚠ `[不稳定渠道]` ("unstable channel") hosts `gemini-3.6-flash` and
`gemini-3.1-pro`. Cheap and newest, but latencies of 19–66s were measured. Do not
put them on an interactive path.
