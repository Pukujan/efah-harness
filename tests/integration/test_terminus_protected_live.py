"""GATE-D1-08 A1 against the running pair of instances.

The owner measured HTTP 401 for the main admin credential against
``localhost:6364`` by hand on 2026-08-02. This encodes that measurement so it
stays true: if a later change wires the builder credential into the protected
instance, this test turns red.

**The failure mode this guards against is the fix.** GATE-D1-08's
``remediation_must_not_include`` is explicit: a red here is never repaired by
granting access. A 200 from the main credential is a hard failure.
"""

from __future__ import annotations

import os

import pytest

from integrations.protected_identity import (
    PROTECTED_DATABASE,
    PROTECTED_ENDPOINT,
    AliasView,
    OwnerAuditRequest,
    ProtectedIdentityAccessError,
    ProtectedModelIdentity,
    probe_credential_against_protected,
    protected_store_from_env,
)
from integrations.terminusdb import TerminusAuthError, TerminusClient, TerminusConfig

MAIN_ENDPOINT = "http://localhost:6363"


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the live TerminusDB tests need it")
    return value


async def test_main_admin_credential_is_refused_by_the_protected_instance():
    """The measurement, encoded: main credential -> :6364 -> 401."""
    main_password = require_env("TERMINUSDB_ADMIN_PASS")
    result = await probe_credential_against_protected(
        main_password, actor="builder/main-admin", endpoint=PROTECTED_ENDPOINT
    )
    assert result.status == 401, (
        f"the main builder credential reached {PROTECTED_ENDPOINT} with HTTP {result.status}; "
        "GATE-D1-08 fails and must NOT be repaired by granting access"
    )
    assert result.is_denied
    assert result.api_error_type == "api:IncorrectAuthenticationError"
    assert result.actor == "builder/main-admin"
    assert result.probed_at


async def test_protected_credential_is_refused_by_the_main_instance():
    """Isolation is symmetric: the protected credential is not a master key."""
    protected_password = require_env("TERMINUSDB_PROTECTED_PASS")
    client = TerminusClient(TerminusConfig(endpoint=MAIN_ENDPOINT, password=protected_password))
    try:
        with pytest.raises(TerminusAuthError) as exc:
            await client.info()
    finally:
        await client.aclose()
    assert exc.value.status == 401


async def test_store_asserts_isolation_and_returns_the_transcript():
    require_env("TERMINUSDB_PROTECTED_PASS")
    main_password = require_env("TERMINUSDB_ADMIN_PASS")
    store = protected_store_from_env()
    async with store:
        result = await store.assert_credential_is_isolated(
            main_password, actor="builder/main-admin"
        )
    assert result.status == 401
    assert result.endpoint == PROTECTED_ENDPOINT


async def test_alias_view_never_carries_the_real_identity_but_owner_audit_does():
    """Contract Section 11.2: aliases to the task side, identity to the owner."""
    require_env("TERMINUSDB_PROTECTED_PASS")
    store = protected_store_from_env(database=PROTECTED_DATABASE)
    async with store:
        await store.ensure_ready()
        identity = ProtectedModelIdentity(
            alias="test-alias-z99",
            provider="test-provider",
            model_id="test-model-9",
            gateway="eval",
            configuration_hash="sha256:test",
            role="integration-test",
            gate_bearing=True,
        )
        await store.record_mapping(identity)

        view = await store.alias_view("test-alias-z99")
        assert isinstance(view, AliasView)
        assert view.alias == "test-alias-z99"
        assert "test-provider" not in repr(view)
        assert "test-model-9" not in repr(view)

        assert "test-alias-z99" in await store.known_aliases()

        revealed = await store.reveal_for_owner_audit(
            "test-alias-z99",
            OwnerAuditRequest(owner_identity="Kujan", reason="integration test audit"),
        )
        assert revealed is not None
        assert revealed.provider == "test-provider"
        assert revealed.model_id == "test-model-9"

        trail = await store.audit_trail("test-alias-z99")
        assert trail, "a reveal must leave an audit record"
        assert trail[-1]["owner_identity"] == "Kujan"
        assert trail[-1]["reason"] == "integration test audit"


async def test_reveal_requires_a_reason():
    require_env("TERMINUSDB_PROTECTED_PASS")
    with pytest.raises(ProtectedIdentityAccessError):
        OwnerAuditRequest(owner_identity="Kujan", reason="")


async def test_unknown_alias_reveals_nothing():
    require_env("TERMINUSDB_PROTECTED_PASS")
    store = protected_store_from_env()
    async with store:
        await store.ensure_ready()
        assert await store.alias_view("no-such-alias-000") is None
        assert (
            await store.reveal_for_owner_audit(
                "no-such-alias-000", OwnerAuditRequest(owner_identity="Kujan", reason="audit")
            )
            is None
        )


async def test_pack_model_policy_identities_live_only_on_the_protected_side():
    """Every alias in the pack resolves to a real identity here, and only here."""
    require_env("TERMINUSDB_PROTECTED_PASS")
    from pathlib import Path

    import yaml

    policy = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "project-pack" / "model-policy.yaml").read_text()
    )
    store = protected_store_from_env()
    async with store:
        await store.ensure_ready()
        stored = await store.seed_from_model_policy(policy)
        assert len(stored) == len(policy["aliases"])

        judge = policy["aliases"]["judge"]
        revealed = await store.reveal_for_owner_audit(
            judge["alias"], OwnerAuditRequest(owner_identity="Kujan", reason="GATE-D1-06 audit")
        )
        assert revealed is not None
        assert revealed.model_id == judge["litellm_model"]
        assert revealed.provider == judge["family"]
        assert revealed.gate_bearing is True

        view = await store.alias_view(judge["alias"])
        assert view is not None and view.gateway == "eval"


def test_the_protected_password_is_not_the_main_password():
    """If these were equal the isolation would be an accident of configuration."""
    main = os.environ.get("TERMINUSDB_ADMIN_PASS")
    protected = os.environ.get("TERMINUSDB_PROTECTED_PASS")
    if not main or not protected:
        pytest.skip("both TerminusDB credentials must be set")
    assert main != protected
