# DEC-002 — Gate-bearing model traffic routes through the eval gateway, never production

**Decision ID:** DEC-002
**Bound to:** EFAH-CONTRACT-001 v1.0
**Class:** OWNER_RISK_ACCEPTANCE (recorded pre-launch)
**Status:** DECIDED
**Date:** 2026-08-01
**Evidence:** owner's `EVALENDPOINT.md`, `CONFIGURATIONGUIDE.md`, `MODELS.md` (measured 2026-08-01)

---

## The decision

Every role whose output feeds a **gate** routes through the eval LiteLLM
deployment (`litellm-eval-production.up.railway.app`). Roles that merely produce
candidate work route through production
(`litellm-production-8656.up.railway.app`).

| Gateway | Roles |
|---|---|
| production | researcher, research_challenger, planner, plan_challenger, implementer, integration_verifier |
| eval | visible_test_author, sealed_holdout_author, mutant_author, oracle_author, adversarial_critic, judge, evidence_auditor, contract_compliance_auditor, release_verifier |

Routing a gate-bearing role to production is `FAILED_PROVENANCE`.

## Why this is a contract matter, not an ops preference

The owner's own guide already says *"never collect evaluation evidence through
the production gateway."* The contract makes it enforceable.

Contract Section 18 requires every model run to record a configuration hash plus
input and output hashes. Section 17.4 requires a trusted oracle to have a
deterministic verdict path. The production gateway defeats both, in four ways
that all fail **silently**:

1. **`num_retries: 5` at two layers.** The recorded run is not the run that
   happened.
2. **Pooling — up to 3 routes per alias.** A failed call silently re-rolls onto
   another upstream key. This never appears in `num_retries`. It is a retry by
   another name, and it means "one alias" does not mean "one model instance."
3. **Cooldowns.** `allowed_fails` / `cooldown_time` park a failing route, so the
   caller receives a *router* error instead of the true upstream error. The
   failure class recorded is wrong.
4. **`drop_params: true`.** LiteLLM strips any parameter the upstream rejects
   without telling the caller. A run recorded as `reasoning_effort: xhigh` may
   have executed without it. The evidence says one thing; the execution was
   another.

None of these throw. All of them produce a green result with a false provenance
record — which is precisely the failure mode contract Section 18 exists to
prevent ("done" without named evidence is invalid; here the evidence is named
but wrong).

## What production is still correct for

Candidate production. An implementer that hits a transient 503 should retry and
fail over — that is what makes the build finish inside 48 hours. Its output is
not evidence; it is a candidate that the assurance path then evaluates through
the eval gateway. The split is the point.

## Client-side obligation

The proxy cannot stop a client from retrying. Both the OpenAI and Anthropic SDKs
default to `max_retries=2`, and `urllib3.Retry` plus most `HTTPAdapter` presets
retry by default. Every eval-path client must set `max_retries=0` and
`timeout=120`, and any shared session object must be checked. This is the
trap that cannot be fixed server-side and is the easiest to miss.

## Preflight obligation

Before every evaluation campaign:

```bash
python validate_eval_config.py --url https://litellm-eval-production.up.railway.app
```

Must exit 0. The live half calls `__canary_invalid` — a route pointing at a
nonexistent upstream model — and asserts it fails **fast** (measured 1.22s; with
5 retries at `retry_after: 2` the same failure takes ≥10s) and returns an
*error*. A 200 would mean something silently fell back.

The eval service must also remain DB-less. If a `DATABASE_URL` appears on it,
delete it: DB-less is what stops it reading or writing production's virtual-key
store.

## Accepted consequences

- Assurance runs have no retry safety net. A transient upstream failure surfaces
  as a real failure. That is intended — a 429 or 503 from the eval gateway is
  evidence, not noise to be hidden.
- Because there is no `max_parallel_requests` on the eval service, the harness
  must throttle globally to stay under ckff's **100 req/min account-wide** cap.
  Queueing inside the proxy would hide 429s that are themselves evidence.
- Two master keys must be maintained. They must not be equal.

## Verified 2026-08-02, and one accepted consequence

Isolation confirmed live: the production key does not authenticate against the
eval gateway, and the eval key returns 401 against production.

The eval gateway rejects foreign keys with 400 `no_db_connection`, not 401.
This is by design, not a weak check. The eval service is deliberately DB-less
(DEC-002 section "The eval service must also remain DB-less"; environments.yaml
lines 85-88, `must_remain_dbless: true`). LiteLLM special-cases the master key
so it needs no DB lookup; every other credential fails at the absent database.
Absence of a key store IS the isolation mechanism. Attaching a DATABASE_URL to
the eval deployment would remove it and is forbidden.

Accepted consequence: the eval side supports exactly one master credential --
no virtual keys, no per-role credentials, no per-key budgets or rate limits,
all of which require the database. Every gate-bearing role therefore shares one
credential on that gateway.

This does NOT weaken role separation. Separation is enforced by the harness
model router and the protected TerminusDB identity database (contract Sections
11.1, 11.2), not by LiteLLM key scoping -- LiteLLM never learns which role is
calling. What is genuinely lost is per-role spend attribution and rate limiting
on the eval path. Record that in the final evidence package as honest debt.

## Owner signature

```
Decided by:    Kujan (github: Pukujan)
Date:          2026-08-02
Contract bind: EFAH-CONTRACT-001 v1.0
Status:        SIGNED
```
