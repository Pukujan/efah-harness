"""T-031 / DEC-002 — the gateway split, enforced rather than documented.

DEC-002 is an owner decision bound to the contract: gate-bearing roles route
through the eval LiteLLM deployment, candidate-producing roles through
production, and routing a gate-bearing role to production is
``FAILED_PROVENANCE``. It fails *silently* in production -- retries, pooling,
cooldowns and ``drop_params`` all produce a green result with a false provenance
record -- so the check has to live in code.

The client-side obligation is the trap that cannot be fixed server-side: both
the OpenAI and Anthropic SDKs default to ``max_retries=2`` and most HTTP adapter
presets retry by default, so an eval client that inherits a default voids the
zero-retry guarantee from outside the proxy.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from integrations.secrets import SecretResolver
from models.errors import FailedProvenanceError
from models.eval_preflight import static_checks, validate_eval_config
from models.gateway import (
    CANARY_MODEL,
    GatewayClass,
    LiteLLMGateway,
    assert_gateway_for_role,
    gateway_class_for_role,
    transport_retries,
)
from models.policy import DEFAULT_POLICY_PATH, ModelPolicy, load_model_policy
from models.throttle import GlobalThrottle

DEC_002 = (
    Path(__file__).resolve().parents[2]
    / "project-pack/evidence/owner-documents/DEC-002-eval-gateway-for-gate-bearing-roles.md"
)

FAKE_ENV = {
    "LITELLM_MASTER_KEY": "sk-test-production",
    "LITELLM_EVAL_MASTER_KEY": "sk-test-eval",
}


def make_gateway(tmp_path, handler=None, **kwargs) -> LiteLLMGateway:
    return LiteLLMGateway(
        resolver=SecretResolver(environ=dict(FAKE_ENV)),
        transport=httpx.MockTransport(handler) if handler else None,
        throttle=GlobalThrottle(
            max_requests_per_minute=90,
            min_interval_seconds=0.0,
            state_path=tmp_path / "throttle.json",
        ),
        **kwargs,
    )


@pytest.fixture
def policy() -> ModelPolicy:
    return load_model_policy()


def dec_002_role_table() -> dict[str, set[str]]:
    """Parse the owner decision itself, so the pack cannot drift away from it."""
    table: dict[str, set[str]] = {}
    for line in DEC_002.read_text().splitlines():
        match = re.match(r"^\|\s*(production|eval)\s*\|(.+)\|\s*$", line)
        if match:
            roles = {r.strip() for r in match.group(2).split(",") if r.strip()}
            table[match.group(1)] = roles
    return table


# -- the split itself ------------------------------------------------------
def test_the_pack_matches_the_owner_decision_exactly(policy):
    table = dec_002_role_table()
    assert table, "DEC-002 role table did not parse"
    assert set(policy.gateway_routing.gate_bearing_roles) == table["eval"]
    assert set(policy.gateway_routing.candidate_roles) == table["production"]


@pytest.mark.parametrize(
    "role",
    [
        "visible_test_author",
        "sealed_holdout_author",
        "mutant_author",
        "oracle_author",
        "adversarial_critic",
        "judge",
        "evidence_auditor",
        "contract_compliance_auditor",
        "release_verifier",
    ],
)
def test_gate_bearing_roles_are_eval_only(role):
    assert gateway_class_for_role(role) is GatewayClass.EVAL
    with pytest.raises(FailedProvenanceError, match="DEC-002"):
        assert_gateway_for_role(role, GatewayClass.PRODUCTION)
    assert assert_gateway_for_role(role, GatewayClass.EVAL) is GatewayClass.EVAL


@pytest.mark.parametrize(
    "role",
    [
        "researcher",
        "research_challenger",
        "planner",
        "plan_challenger",
        "implementer",
        "integration_verifier",
    ],
)
def test_candidate_roles_are_production(role):
    assert gateway_class_for_role(role) is GatewayClass.PRODUCTION
    with pytest.raises(FailedProvenanceError):
        assert_gateway_for_role(role, GatewayClass.EVAL)


def test_the_failure_message_names_the_silent_corruption(policy):
    with pytest.raises(FailedProvenanceError) as excinfo:
        assert_gateway_for_role("judge", "production")
    message = str(excinfo.value)
    assert "FAILED_PROVENANCE" in message
    assert "drop_params" in message


def test_client_for_role_cannot_be_pointed_at_the_wrong_gateway(tmp_path):
    gateway = make_gateway(tmp_path)
    _client, endpoint = gateway.client_for_role("judge")
    assert endpoint.gateway_class is GatewayClass.EVAL
    assert "eval" in endpoint.base_url
    with pytest.raises(FailedProvenanceError):
        gateway.client_for_role("judge", gateway=GatewayClass.PRODUCTION)


async def test_a_gate_bearing_call_cannot_be_forced_onto_production(tmp_path):
    gateway = make_gateway(tmp_path, handler=lambda r: httpx.Response(200, json={}))
    with pytest.raises(FailedProvenanceError):
        await gateway.chat_completion(
            role="judge",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=512,
            gateway=GatewayClass.PRODUCTION,
        )
    await gateway.aclose()


# -- the client-side obligation -------------------------------------------
def test_the_eval_client_is_constructed_with_zero_retries(tmp_path):
    """The trap DEC-002 calls "the easiest to miss"."""
    gateway = make_gateway(tmp_path)
    eval_client = gateway.client(GatewayClass.EVAL)
    assert gateway.endpoints[GatewayClass.EVAL].max_retries == 0
    assert transport_retries(eval_client) == 0
    assert eval_client.efah_max_retries == 0


def test_the_eval_client_timeout_is_120_seconds(tmp_path):
    gateway = make_gateway(tmp_path)
    client = gateway.client(GatewayClass.EVAL)
    assert gateway.endpoints[GatewayClass.EVAL].timeout_seconds == 120
    assert client.timeout.read == 120.0
    assert client.timeout.connect == 120.0
    assert client.timeout.write == 120.0


def test_the_two_gateways_do_not_share_a_session_object(tmp_path):
    gateway = make_gateway(tmp_path)
    assert gateway.client(GatewayClass.EVAL) is not gateway.client(GatewayClass.PRODUCTION)
    assert gateway.client(GatewayClass.EVAL).base_url != gateway.client(GatewayClass.PRODUCTION).base_url


def test_the_two_gateways_do_not_share_a_credential(tmp_path):
    gateway = make_gateway(tmp_path)
    assert gateway.api_key(GatewayClass.EVAL) != gateway.api_key(GatewayClass.PRODUCTION)


def test_reusing_the_production_key_on_eval_is_refused(tmp_path):
    same = {"LITELLM_MASTER_KEY": "sk-same", "LITELLM_EVAL_MASTER_KEY": "sk-same"}
    with pytest.raises(FailedProvenanceError, match="must not"):
        LiteLLMGateway(
            resolver=SecretResolver(environ=same),
            throttle=GlobalThrottle(
                max_requests_per_minute=90,
                min_interval_seconds=0.0,
                state_path=tmp_path / "t.json",
            ),
        )


def test_a_pack_that_asks_for_eval_retries_is_refused(tmp_path):
    data = copy.deepcopy(yaml.safe_load(DEFAULT_POLICY_PATH.read_text()))
    data["gateway_routing"]["eval"]["client_requirements"]["sdk_max_retries"] = 2
    path = tmp_path / "model-policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(FailedProvenanceError, match="sdk_max_retries"):
        LiteLLMGateway(
            policy=ModelPolicy.load(path),
            resolver=SecretResolver(environ=dict(FAKE_ENV)),
            throttle=GlobalThrottle(
                max_requests_per_minute=90,
                min_interval_seconds=0.0,
                state_path=tmp_path / "t.json",
            ),
        )


def test_the_production_client_may_retry_at_the_harness_layer(tmp_path, policy):
    """Candidate production wants failover; its output is not evidence."""
    gateway = make_gateway(tmp_path)
    assert gateway.endpoints[GatewayClass.PRODUCTION].max_retries == (
        policy.retry_policy.max_retries_per_work_unit
    )
    assert transport_retries(gateway.client(GatewayClass.PRODUCTION)) == 0


# -- the preflight obligation ---------------------------------------------
async def test_an_eval_call_without_a_preflight_is_refused(tmp_path):
    gateway = make_gateway(
        tmp_path, handler=lambda r: httpx.Response(200, json={}), require_eval_preflight=True
    )
    with pytest.raises(FailedProvenanceError, match="preflight"):
        await gateway.chat_completion(
            role="judge", messages=[{"role": "user", "content": "hi"}], max_tokens=512
        )
    await gateway.aclose()


async def test_a_passing_preflight_authorises_eval_dispatch(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == CANARY_MODEL:
            return httpx.Response(503, text="No available channel")
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    gateway = make_gateway(tmp_path, handler=handler, require_eval_preflight=True)
    result = await validate_eval_config(gateway=gateway)
    assert result.passed
    assert result.canary is not None and result.canary.errored and result.canary.fast
    assert gateway.eval_preflight_valid
    response = await gateway.chat_completion(
        role="judge", messages=[{"role": "user", "content": "hi"}], max_tokens=512
    )
    assert response.gateway == "eval"
    assert response.alias == "judge-j03"
    await gateway.aclose()


async def test_a_canary_that_returns_200_fails_the_preflight(tmp_path):
    """A 200 means something silently fell back -- the exact failure mode."""
    gateway = make_gateway(
        tmp_path,
        handler=lambda r: httpx.Response(200, json={"choices": []}),
        require_eval_preflight=True,
    )
    result = await validate_eval_config(gateway=gateway)
    assert not result.passed
    assert any(c.name == "canary_returns_error" and not c.passed for c in result.checks)
    assert not gateway.eval_preflight_valid
    await gateway.aclose()


def test_the_static_preflight_checks_cover_the_dec_002_client_obligations(tmp_path):
    gateway = make_gateway(tmp_path)
    names = {check.name for check in static_checks(gateway)}
    assert {
        "eval_client_zero_retries",
        "eval_client_timeout_120",
        "eval_transport_retries_zero",
        "no_shared_session_object",
        "separate_master_keys",
        "gate_bearing_roles_on_eval",
    } <= names


def test_only_the_eval_endpoint_is_evidence_grade(tmp_path):
    gateway = make_gateway(tmp_path)
    assert gateway.endpoints[GatewayClass.EVAL].valid_for_evidence is True
    assert gateway.endpoints[GatewayClass.PRODUCTION].valid_for_evidence is False
