"""T-032 / GATE-D1-06 — blinded model identity (Sections 11.2, 12.3).

A1  no agent-visible payload contains a vendor or model name
A2  task and audit records reference aliases, not real identities
A5  no agent receives another agent's prestige ranking or cost tier

The protected-store half (A3/A4) is exercised against the port; the TerminusDB
adapter for the isolated instance on ``localhost:6364`` is WS-B's file
``src/integrations/protected_identity.py`` and is not created here.
"""

from __future__ import annotations

import pytest

from models.availability import CapabilityRegistry, ModelCapability
from models.blinding import (
    ModelIdentity,
    PackIdentityStore,
    ProtectedIdentityStore,
    assert_task_payload_blinded,
    scan_task_payload,
    seed_protected_identity,
)
from models.errors import BlindingViolationError, ProtectedAccessError
from models.policy import load_model_policy
from models.router import ModelRouter, RoutingRequest
from workers.session import WorkerSession, WorkUnit


@pytest.fixture
def policy():
    return load_model_policy()


@pytest.fixture
def router(policy):
    registry = CapabilityRegistry()
    for row in policy.roles.values():
        registry.record(ModelCapability(alias=row.alias, gateway=row.gateway, available=True))
    return ModelRouter(policy=policy, capabilities=registry)


def task_payload(router, role: str) -> dict:
    """The payload a worker actually receives: routing decision + prompt."""
    decision = router.route(RoutingRequest(role=role))
    work_unit = WorkUnit(
        task_id="T-1",
        role=role,
        instructions="Write the failing test first, then the code that passes it.",
        inputs={"requirement_id": "R-014", "acceptance": "deterministic oracle"},
    )
    session = WorkerSession.open(work_unit, alias=decision.alias)
    return {"routing": decision.as_body(), "messages": session.messages()}


# -- A1 / A2 ---------------------------------------------------------------
def test_task_facing_payload_contains_an_alias_and_no_real_model_id(router, policy):
    """GATE-D1-06: the payload names ``implementer-i12``, never ``gpt-5.6-luna``."""
    payload = task_payload(router, "implementer")
    assert payload["routing"]["alias"] == "implementer-i12"
    assert scan_task_payload(payload, policy) == []
    rendered = str(payload).lower()
    assert "gpt-5.6-luna" not in rendered
    assert policy.role("implementer").family not in rendered


def test_every_mapped_role_produces_a_blinded_payload(router, policy):
    for role in policy.roles:
        assert_task_payload_blinded(task_payload(router, role), policy)


def test_the_scanner_catches_a_leaked_model_id(router, policy):
    payload = task_payload(router, "judge")
    payload["messages"][0]["content"] += "\n(you are running on [ds2] deepseek-v4-pro)"
    findings = scan_task_payload(payload, policy)
    assert findings, "a leaked model id must be detected"
    with pytest.raises(BlindingViolationError):
        assert_task_payload_blinded(payload, policy)


def test_the_scanner_catches_a_leaked_vendor_word(router, policy):
    payload = task_payload(router, "researcher")
    payload["messages"][0]["content"] += " Ask the Anthropic model to confirm."
    with pytest.raises(BlindingViolationError):
        assert_task_payload_blinded(payload, policy)


def test_the_scanner_is_not_trigger_happy_about_ordinary_english(policy):
    """"code", "max", "flash" are fragments of real model ids and ordinary words.

    A scanner that rejects the word "code" in a software-engineering harness gets
    switched off, and then the gate is decorative.
    """
    payload = {
        "messages": [
            {"role": "user", "content": "Refactor the code, cap max retries, flash the firmware."}
        ]
    }
    assert scan_task_payload(payload, policy) == []


# -- A5: prestige and cost tier -------------------------------------------
@pytest.mark.parametrize(
    "leak",
    [
        {"tier": "frontier"},
        {"cost_tier": "cheap"},
        {"price": "$5.00/$25.00 per M"},
        {"prestige_rank": 1},
        {"family": "openai"},
        {"vendor": "google"},
        {"measured": {"median_latency_s": 1.7}},
    ],
)
def test_prestige_and_cost_tier_fields_are_rejected(router, policy, leak):
    payload = task_payload(router, "planner")
    payload["routing"].update(leak)
    with pytest.raises(BlindingViolationError):
        assert_task_payload_blinded(payload, policy)


def test_routing_decision_has_no_tier_or_price_field(router):
    body = router.route(RoutingRequest(role="release_verifier")).as_body()
    assert not {"tier", "cost_tier", "price", "prestige", "family", "vendor", "model"} & set(body)


# -- protected identity port ----------------------------------------------
async def test_pack_identity_store_satisfies_the_protected_port(policy):
    store = PackIdentityStore(policy)
    assert isinstance(store, ProtectedIdentityStore)
    identity = await store.resolve_alias("implementer-i12")
    assert isinstance(identity, ModelIdentity)
    assert identity.litellm_model == "gpt-5.6-luna"
    assert identity.family == "openai"


async def test_an_unprivileged_caller_cannot_reveal_an_identity(policy):
    store = PackIdentityStore(policy)
    with pytest.raises(ProtectedAccessError):
        await store.resolve_alias("judge-j03", caller="implementer")


def test_a_model_identity_never_renders_its_real_model_id(policy):
    identity = ModelIdentity(
        alias="holdout-h01", litellm_model="claude-opus-4-8", family="anthropic", gateway="eval"
    )
    assert "claude" not in repr(identity)
    assert "claude" not in str(identity)
    assert scan_task_payload({"note": repr(identity)}, policy) == []


async def test_seeding_records_every_alias_in_the_protected_store(policy):
    recorded: dict[str, ModelIdentity] = {}

    class Recorder:
        async def resolve_alias(self, alias: str) -> ModelIdentity:
            return recorded[alias]

        async def record_mapping(self, alias, litellm_model, family, gateway, tier="unspecified"):
            recorded[alias] = ModelIdentity(
                alias=alias, litellm_model=litellm_model, family=family, gateway=gateway, tier=tier
            )

    aliases = await seed_protected_identity(Recorder(), policy)
    assert set(aliases) == policy.aliases
    assert recorded["judge-j03"].family == "deepseek"
