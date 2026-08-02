# DEC-302 — LiteLLM is integrated at its HTTP surface, not through a client SDK

**Bound to:** EFAH-CONTRACT-001 v1.1 · §4 (selected stack), §5.1 (adapters),
§14.2 (dependency-first gate) · DEC-002
**Class:** `BUILD_VS_INTEGRATE`
**Date:** 2026-08-02 · **Workstream:** WS-D
**Status:** RECORDED

## The question

`dependency-policy.yaml → selected_stack.model_gateway` selects LiteLLM with
`use_existing_config: true` and `replaceable: false`. The harness talks to it
over `httpx` against `/v1/chat/completions` rather than importing the `litellm`
Python package or a vendor SDK. Is the selected dependency actually being used?

## Decision

Yes — the selected dependency is the **two running LiteLLM deployments**, and
they are integrated at the interface they expose. The gateway module is an
adapter (§5.1), not a reimplementation.

## Reasoning

1. **The dependency is the proxy, not a library.** The owner runs LiteLLM as two
   configured services. All routing, pooling, cooldown, retry, `drop_params` and
   key policy live in those deployments and are read, not reproduced. §4
   explicitly forbids redesigning provider access, and `environments.yaml` sets
   `redesign_permitted: false` on both.

2. **The client-side obligation requires control of the transport.** DEC-002:
   "Both the OpenAI and Anthropic SDKs default to `max_retries=2`, and
   `urllib3.Retry` plus most `HTTPAdapter` presets retry by default. Every
   eval-path client must set `max_retries=0` and `timeout=120`, and any shared
   session object must be checked. This is the trap that cannot be fixed
   server-side." An SDK whose retry behaviour is a default we inherit is the
   trap. `httpx.AsyncHTTPTransport(retries=0)` is constructed explicitly and the
   live connection pool is read back and asserted in the preflight.

3. **Vendor neutrality.** GATE-D1-07 forbids a vendor SDK import outside
   `src/workers/adapters/`. The HTTP surface is vendor-neutral by construction:
   the same code path reaches all seven configured families and runs with every
   Anthropic credential unset.

4. **No new dependency.** `httpx` is already pinned in `pyproject.toml` and is
   LangGraph's transitive HTTP client. Adding the `litellm` package would add a
   large dependency whose only role would be to format a JSON body.

## What is *not* reimplemented

Provider selection, key groups, pooling, cooldowns, failover, param dropping,
model aliasing upstream, and spend accounting. Those are the proxy's, and the
harness reads their behaviour rather than duplicating it — the preflight's
purpose is precisely to verify the proxy's configured behaviour rather than to
substitute for it.

## Evidence

- `tests/contract/test_gateway_split.py` — retries, timeout, session separation,
  credential separation, preflight gating.
- `tests/integration/test_litellm_live.py` — one real call through each
  deployment, plus the `__canary_invalid` fast-fail probe.
