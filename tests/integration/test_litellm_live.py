"""Live verification against both real LiteLLM deployments.

Opt-in: ``EFAH_LIVE_TESTS=1``. These tests spend real requests against an
account-wide 100 req/min cap shared by every worktree on this host, so they are
deliberately few and strictly serial -- the throttle is the same process-wide
limiter the harness uses, not a test double. An unthrottled sweep would
self-inflict 429s, and a self-inflicted 429 recorded as a model failure is
fabricated evidence.

Run::

    EFAH_LIVE_TESTS=1 python -m pytest tests/integration/test_litellm_live.py -q -s
"""

from __future__ import annotations

import os

import pytest

from governance.states import TaskState
from models.availability import AvailabilityProbe, CapabilityRegistry
from models.eval_preflight import validate_eval_config
from models.gateway import GatewayClass, LiteLLMGateway
from models.policy import load_model_policy
from models.router import ModelRouter, RoutingRequest
from workers.registry import build_registry
from workers.session import WorkUnit

pytestmark = pytest.mark.skipif(
    os.environ.get("EFAH_LIVE_TESTS") != "1",
    reason="live gateway tests are opt-in: set EFAH_LIVE_TESTS=1",
)


@pytest.fixture(scope="module")
def policy():
    return load_model_policy()


@pytest.fixture
async def gateway(policy):
    gw = LiteLLMGateway(policy=policy)
    yield gw
    await gw.aclose()


async def test_eval_preflight_passes_against_the_live_gateway(gateway):
    """DEC-002: ``__canary_invalid`` must return an error, fast."""
    result = await validate_eval_config(gateway=gateway)
    for check in result.checks:
        print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
    assert result.canary is not None
    assert result.canary.http_status != 200, "a 200 means something silently fell back"
    assert result.canary.elapsed_seconds < 5.0, "a slow failure means hidden retries"
    assert result.passed


async def test_a_candidate_role_completes_through_the_production_gateway(gateway, policy):
    registry = CapabilityRegistry()
    probe = AvailabilityProbe(gateway, policy, registry)
    capability = await probe.probe_role("integration_verifier")
    print(f"  probe integration_verifier -> {capability}")
    assert capability.available, capability.detail

    router = ModelRouter(policy=policy, capabilities=registry)
    decision = router.route(RoutingRequest(role="integration_verifier"))
    assert decision.gateway == GatewayClass.PRODUCTION.value

    adapter = build_registry(gateway, policy=policy).default()
    assert adapter.name == "litellm"
    outcome = await adapter.execute(
        WorkUnit(
            task_id="LIVE-prod",
            role="integration_verifier",
            instructions="Reply with exactly the two characters: OK",
            max_tokens=512,
        ),
        decision,
    )
    print(f"  production alias={outcome.alias} latency={outcome.latency_seconds}s text={outcome.text!r}")
    assert outcome.state is TaskState.CANDIDATE_COMPLETE
    assert outcome.alias == "wiring-w05"
    assert outcome.output_hash.startswith("sha256:")


async def test_a_gate_bearing_role_completes_through_the_eval_gateway(gateway, policy):
    preflight = await validate_eval_config(gateway=gateway)
    assert preflight.passed

    registry = CapabilityRegistry()
    probe = AvailabilityProbe(gateway, policy, registry)
    capability = await probe.probe_role("evidence_auditor")
    print(f"  probe evidence_auditor -> {capability}")
    assert capability.available, capability.detail
    assert capability.emitted_tool_call, "512 tokens must be enough to emit a tool call"

    router = ModelRouter(policy=policy, capabilities=registry)
    decision = router.route(RoutingRequest(role="evidence_auditor"))
    assert decision.gateway == GatewayClass.EVAL.value
    assert decision.max_retries == 0

    adapter = build_registry(gateway, policy=policy).default()
    outcome = await adapter.execute(
        WorkUnit(
            task_id="LIVE-eval",
            role="evidence_auditor",
            instructions="Reply with exactly the two characters: OK",
            max_tokens=512,
        ),
        decision,
    )
    print(f"  eval alias={outcome.alias} latency={outcome.latency_seconds}s text={outcome.text!r}")
    assert outcome.state is TaskState.CANDIDATE_COMPLETE
    assert outcome.alias == "auditor-a07"
    assert outcome.configuration_hash.startswith("sha256:")


async def test_the_production_key_does_not_authenticate_against_eval(gateway):
    """DEC-002 "Verified 2026-08-02": the eval service is DB-less, so a foreign
    credential fails at the absent key store. Absence of a key store *is* the
    isolation mechanism."""
    client = gateway.client(GatewayClass.EVAL)
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 512},
        headers={"Authorization": f"Bearer {gateway.api_key(GatewayClass.PRODUCTION)}"},
    )
    print(f"  production key against eval gateway -> HTTP {response.status_code}")
    assert response.status_code >= 400
