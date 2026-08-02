"""GATE-D1-08 evidence that does not need the network.

Two things are proved here:

1. **Structural containment.** ``src/integrations/protected_identity.py`` is the
   only module that names the protected port or the protected credential. If a
   second module ever reaches for either, the build fails -- which is what makes
   "only the model router's protected map may touch this" enforceable rather
   than aspirational.
2. **Type-level blinding.** The task-facing :class:`AliasView` has no field that
   can carry a real vendor or model id, so a leak cannot happen by a caller
   printing the wrong object.

The 401 itself is measured live in
``tests/integration/test_terminus_protected_live.py``.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from integrations.protected_identity import (
    PROTECTED_DATABASE,
    PROTECTED_ENDPOINT,
    AliasView,
    IsolationProbeResult,
    OwnerAuditRequest,
    ProtectedIdentityAccessError,
    ProtectedIdentityStore,
    ProtectedModelIdentity,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
OWNER = SRC / "integrations" / "protected_identity.py"

#: Anything that would give another module a route to the protected instance.
PROTECTED_MARKERS = ("6364", "TERMINUSDB_PROTECTED_PASS", "efah_protected")


def test_only_the_protected_adapter_names_the_protected_instance():
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == OWNER:
            continue
        text = path.read_text()
        hits = [marker for marker in PROTECTED_MARKERS if marker in text]
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: {hits}")
    assert not offenders, (
        "contract Section 11.2: only integrations/protected_identity.py may reach the "
        f"protected instance, but found routes in {offenders}"
    )


def test_the_protected_client_is_name_mangled_and_not_exposed():
    """A caller holding the client holds the credential's reach."""
    public = [name for name in dir(ProtectedIdentityStore) if not name.startswith("_")]
    assert "client" not in public
    tree = ast.parse(OWNER.read_text())
    store = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ProtectedIdentityStore"
    )
    assigned = {
        target.attr
        for node in ast.walk(store)
        for target in getattr(node, "targets", [])
        if isinstance(target, ast.Attribute)
    }
    assert "__client" in assigned, "the TerminusDB client must stay private to the store"


def test_alias_view_cannot_carry_a_real_identity():
    fields = {f.name for f in dataclasses.fields(AliasView)}
    assert fields == {"alias", "role", "gateway", "gate_bearing"}
    assert "provider" not in fields and "model_id" not in fields


def test_real_identity_type_is_separate_from_the_task_facing_type():
    real = {f.name for f in dataclasses.fields(ProtectedModelIdentity)}
    assert {"provider", "model_id"} <= real
    assert not issubclass(AliasView, ProtectedModelIdentity)


@pytest.mark.parametrize(
    ("owner", "reason"), [("", "audit"), ("Kujan", ""), ("   ", "audit"), ("Kujan", "  ")]
)
def test_owner_audit_request_requires_identity_and_reason(owner, reason):
    with pytest.raises(ProtectedIdentityAccessError):
        OwnerAuditRequest(owner_identity=owner, reason=reason)


async def test_reveal_refuses_a_duck_typed_audit_context():
    class NotAnAuditRequest:
        owner_identity = "someone"
        reason = "because"

    store = ProtectedIdentityStore(password="unused", endpoint="http://127.0.0.1:1")
    try:
        with pytest.raises(ProtectedIdentityAccessError):
            await store.reveal_for_owner_audit("judge-j03", NotAnAuditRequest())  # type: ignore[arg-type]
    finally:
        await store.aclose()


def test_isolation_probe_result_treats_only_denials_as_denied():
    def probe(status: int) -> IsolationProbeResult:
        return IsolationProbeResult(
            endpoint=PROTECTED_ENDPOINT,
            actor="builder",
            status=status,
            api_error_type=None,
            probed_at="2026-08-02T00:00:00+00:00",
        )

    assert probe(401).is_denied and probe(403).is_denied and probe(404).is_denied
    assert not probe(200).is_denied, "a 200 is a hard failure, not a convenience"


def test_protected_constants_match_the_pack_environment():
    import yaml

    env = yaml.safe_load((ROOT / "project-pack" / "environments.yaml").read_text())
    block = env["environments"]["dev"]["terminusdb_protected"]
    assert block["url"] == PROTECTED_ENDPOINT
    assert block["database"] == PROTECTED_DATABASE
    assert block["main_admin_credential_must_fail"] is True
