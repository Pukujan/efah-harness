"""T-030 — the model router is a deterministic policy service (Section 11.1).

These tests fail if the router hardcodes the alias map, leaks a vendor identity,
routes a gate-bearing role to production, tolerates a role-separation violation,
selects a prohibited or degraded model, or dispatches without an availability
record.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from models.availability import CapabilityRegistry, ModelCapability
from models.errors import (
    AvailabilityProbeRequiredError,
    FailedProvenanceError,
    ModelUnavailableError,
    ProhibitedModelError,
    RoleConflictError,
)
from models.policy import DEFAULT_POLICY_PATH, ModelPolicy, load_model_policy
from models.router import ModelRouter, RoutingRequest

PACK_POLICY = yaml.safe_load(DEFAULT_POLICY_PATH.read_text())


def write_policy(tmp_path: Path, mutate) -> ModelPolicy:
    """Write a mutated copy of the real pack policy and load it."""
    data = copy.deepcopy(PACK_POLICY)
    mutate(data)
    path = tmp_path / "model-policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return ModelPolicy.load(path)


def all_available(policy: ModelPolicy) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for row in policy.roles.values():
        registry.record(ModelCapability(alias=row.alias, gateway=row.gateway, available=True))
    return registry


@pytest.fixture
def policy() -> ModelPolicy:
    return load_model_policy()


@pytest.fixture
def router(policy: ModelPolicy) -> ModelRouter:
    return ModelRouter(policy=policy, capabilities=all_available(policy))


# -- the map comes from the pack ------------------------------------------
def test_alias_map_is_loaded_from_the_pack_not_hardcoded(tmp_path, policy):
    """Change the pack, and the router changes with it."""
    mutated = write_policy(
        tmp_path, lambda d: d["aliases"]["implementer"].__setitem__("alias", "implementer-zz99")
    )
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))
    decision = router.route(RoutingRequest(role="implementer"))
    assert decision.alias == "implementer-zz99"
    assert policy.role("implementer").alias == "implementer-i12"


def test_unknown_role_is_not_routable(router):
    with pytest.raises(FailedProvenanceError):
        router.route(RoutingRequest(role="chief_vibes_officer"))


# -- Section 11.1: alias + configuration version, nothing else -------------
def test_decision_carries_alias_and_configuration_version(router, policy):
    decision = router.route(RoutingRequest(role="researcher"))
    assert decision.alias == "researcher-r17"
    assert decision.configuration_version == policy.configuration_version
    assert decision.policy_hash == policy.policy_hash
    assert decision.decision_hash.startswith("sha256:")


def test_decision_never_exposes_a_real_identity(router, policy):
    for role in policy.roles:
        body = router.route(RoutingRequest(role=role)).as_body()
        flat = " ".join(str(v).lower() for v in body.values())
        for banned in ("gpt", "claude", "gemini", "grok", "deepseek", "qwen", "glm", "anthropic"):
            assert banned not in flat, f"{role}: routing decision leaked {banned!r}"
        assert not {"model", "family", "vendor", "tier", "price"} & set(body)


def test_routing_is_deterministic(router):
    request = RoutingRequest(role="oracle_author", risk_class="high")
    first = router.route(request)
    second = router.route(request)
    assert first.decision_hash == second.decision_hash


# -- DEC-002 --------------------------------------------------------------
def test_gate_bearing_roles_route_to_eval(router, policy):
    for role in policy.gateway_routing.gate_bearing_roles:
        assert router.route(RoutingRequest(role=role)).gateway == "eval"


def test_candidate_roles_route_to_production(router, policy):
    for role in policy.gateway_routing.candidate_roles:
        assert router.route(RoutingRequest(role=role)).gateway == "production"


def test_eval_path_gets_zero_retries_and_a_120s_timeout(router):
    decision = router.route(RoutingRequest(role="sealed_holdout_author"))
    assert decision.max_retries == 0
    assert decision.timeout_seconds == 120


def test_pack_that_contradicts_dec_002_is_unloadable(tmp_path):
    """A role whose alias entry and gateway_routing disagree cannot dispatch."""

    def mutate(data):
        data["aliases"]["judge"]["gateway"] = "production"

    with pytest.raises(FailedProvenanceError):
        write_policy(tmp_path, mutate)


# -- role_incompatibilities -----------------------------------------------
def test_pack_role_separation_holds(router):
    assert [f for f in router.role_separation_findings() if not f.startswith("advisory: ")] == []


@pytest.mark.parametrize(
    "victim,rule_desc",
    [
        ("sealed_holdout_author", "builder != holdout author"),
        ("judge", "builder != final adjudicator"),
    ],
)
def test_implementer_sharing_an_alias_with_a_gate_role_is_a_role_conflict(tmp_path, victim, rule_desc):
    def mutate(data):
        implementer = data["aliases"]["implementer"]
        data["aliases"][victim]["alias"] = implementer["alias"]
        data["aliases"][victim]["family"] = implementer["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))
    with pytest.raises(RoleConflictError, match="role separation"):
        router.route(RoutingRequest(role="implementer"))
    assert rule_desc  # documents which contract rule this covers


def test_critic_sharing_a_family_with_the_implementer_is_a_role_conflict(tmp_path):
    """``must_differ_by_family`` — a same-family critic is not an independent one."""

    def mutate(data):
        data["aliases"]["adversarial_critic"]["family"] = data["aliases"]["implementer"]["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))
    with pytest.raises(RoleConflictError):
        router.route(RoutingRequest(role="adversarial_critic"))


def test_should_differ_rules_are_advisory_not_blocking(tmp_path):
    def mutate(data):
        data["aliases"]["research_challenger"]["family"] = data["aliases"]["researcher"]["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))
    findings = router.role_separation_findings("researcher")
    assert any(f.startswith("advisory: ") for f in findings)
    assert router.route(RoutingRequest(role="researcher")).alias


# -- prohibited and degraded models ---------------------------------------
def test_prohibited_model_cannot_be_mapped_to_a_role(tmp_path):
    def mutate(data):
        data["aliases"]["planner"]["litellm_model"] = "gpt-5.6-sol"  # latency_variance

    with pytest.raises(ProhibitedModelError):
        write_policy(tmp_path, mutate)


def test_wildcard_prohibition_is_honoured(policy):
    assert policy.prohibition_reason("[不稳定渠道] anything-at-all") == "unstable_channel"
    assert policy.prohibition_reason("claude-opus-5") == "cost_trap"
    assert policy.prohibition_reason("gpt-5.6-luna") is None


def test_pack_time_degraded_model_cannot_be_mapped(tmp_path):
    def mutate(data):
        data["aliases"]["planner"]["litellm_model"] = "[aws]glm-5"

    with pytest.raises(ProhibitedModelError):
        write_policy(tmp_path, mutate)


# -- prohibited_aliases and substitution ----------------------------------
def test_prohibited_alias_forces_a_substitute_that_preserves_separation(router, policy):
    decision = router.route(
        RoutingRequest(role="judge", prohibited_aliases=("judge-j03",))
    )
    assert decision.substituted is True
    assert decision.alias != "judge-j03"
    assert decision.gateway == "eval"
    substitute_family = policy.role_for_alias(decision.alias).family
    assert substitute_family != policy.role("implementer").family


def test_substitution_is_deterministic(router):
    request = RoutingRequest(role="judge", prohibited_aliases=("judge-j03",))
    assert router.route(request).alias == router.route(request).alias


def test_no_remaining_alias_is_a_typed_unavailability(router, policy):
    eval_aliases = tuple(
        row.alias for row in policy.roles.values() if row.gateway == "eval"
    )
    with pytest.raises(ModelUnavailableError):
        router.route(RoutingRequest(role="judge", prohibited_aliases=eval_aliases))


# -- availability ---------------------------------------------------------
def test_dispatch_without_a_capability_record_is_refused(policy):
    router = ModelRouter(policy=policy, capabilities=CapabilityRegistry())
    with pytest.raises(AvailabilityProbeRequiredError):
        router.route(RoutingRequest(role="implementer"))


def test_router_without_a_registry_refuses_when_a_probe_is_required(policy):
    router = ModelRouter(policy=policy)
    with pytest.raises(AvailabilityProbeRequiredError):
        router.route(RoutingRequest(role="implementer"))
    assert router.route(RoutingRequest(role="implementer", availability_probe_required=False)).alias


def test_an_alias_probed_unavailable_is_replaced(policy):
    registry = all_available(policy)
    registry.record(
        ModelCapability(alias="implementer-i12", gateway="production", available=False)
    )
    router = ModelRouter(policy=policy, capabilities=registry)
    decision = router.route(RoutingRequest(role="implementer"))
    assert decision.substituted is True
    assert decision.alias != "implementer-i12"
    assert decision.gateway == "production"
