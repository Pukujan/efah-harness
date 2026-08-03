"""Alias blinding and the protected-identity boundary.

Contract Section 12.3: agents see aliases only -- ``researcher-r17``,
``implementer-i12``, ``judge-j03``. No agent may receive another agent's vendor,
model family, prestige ranking, or cost tier. Section 11.2: the real mapping
lives in a *separate protected* TerminusDB instance, and only the owner audit
path may reveal it.

This module owns three things:

* :class:`ProtectedIdentityStore` -- the port. The TerminusDB-backed adapter for
  the protected identity instance is owned by the TerminusDB lane and belongs at
  ``src/integrations/protected_identity.py``. This module deliberately does not
  create that file; it codes against the Protocol so the adapter can be dropped
  in without touching the router.
* :class:`PackIdentityStore` -- the dispatch-side resolver used until (and
  alongside) the protected instance. ``model-policy.yaml`` is owner-held
  configuration read by the dispatch process; it is *not* an agent-visible
  surface. Task participants never receive it.
* the payload scanner -- GATE-D1-06's mechanical check. A payload leaves for a
  worker only after :func:`assert_task_payload_blinded` has seen it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from governance.envelope import utc_now
from models.errors import BlindingViolationError, ProtectedAccessError
from models.policy import ModelPolicy, load_model_policy

#: Vendor words that must never appear in a task-facing payload, independent of
#: what the pack happens to map today. Derived from the model inventory measured
#: on both gateways plus the SDK/vendor names an agent could infer identity from.
VENDOR_TOKENS: frozenset[str] = frozenset(
    {
        "anthropic",
        "claude",
        "openai",
        "gpt",
        "codex",
        "google",
        "gemini",
        "deepseek",
        "xai",
        "grok",
        "qwen",
        "alibaba",
        "zhipu",
        "glm",
        "moonshot",
        "kimi",
        "minimax",
        "mimo",
        "mistral",
        "llama",
        "cohere",
    }
)

#: Fields that leak prestige or cost tier (GATE-D1-06 A5) even when they carry
#: no vendor string at all.
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "litellm_model",
        "model_id",
        "model_name",
        "vendor",
        "provider",
        "family",
        "model_family",
        "tier",
        "cost_tier",
        "price",
        "pricing",
        "prestige",
        "prestige_rank",
        "ranking",
        "measured",
        "api_key",
        "authorization",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class ModelIdentity:
    """The protected mapping for one alias. Never serialised into a payload."""

    alias: str
    litellm_model: str
    family: str
    gateway: str
    tier: str = "unspecified"
    recorded_at: str = field(default_factory=utc_now)
    runs_under_identity: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive, exercised in tests
        return f"ModelIdentity(alias={self.alias!r}, <protected>)"

    def __str__(self) -> str:
        return self.__repr__()


@runtime_checkable
class ProtectedIdentityStore(Protocol):
    """Port for the protected identity instance (contract Section 11.2).

    The TerminusDB lane implements this over the protected instance in
    ``src/integrations/protected_identity.py``. The main-instance credential
    must continue to fail against it.
    """

    async def resolve_alias(self, alias: str) -> ModelIdentity: ...

    async def record_mapping(
        self,
        alias: str,
        litellm_model: str,
        family: str,
        gateway: str,
        tier: str = ...,
    ) -> None: ...


class PackIdentityStore:
    """:class:`ProtectedIdentityStore` backed by the owner's pack policy.

    Used by the dispatch process, which must know the real model id in order to
    address LiteLLM at all. It is not reachable from a worker session: worker
    sessions receive a :class:`~models.router.RoutingDecision`, which carries an
    alias and nothing else.

    ``allow_reveal`` gates the audit path. A caller that is not the dispatch
    service or the owner audit path gets ``PROTECTED_ACCESS``.
    """

    #: Callers permitted to see a real identity (Section 11.2).
    PRIVILEGED_CALLERS = frozenset({"dispatch_service", "owner_audit", "availability_probe"})

    def __init__(self, policy: ModelPolicy | None = None) -> None:
        self._policy = policy or load_model_policy()
        self._extra: dict[str, ModelIdentity] = {}

    async def resolve_alias(self, alias: str, *, caller: str = "dispatch_service") -> ModelIdentity:
        if caller not in self.PRIVILEGED_CALLERS:
            raise ProtectedAccessError(
                f"caller {caller!r} may not resolve alias {alias!r} to a real model identity"
            )
        if alias in self._extra:
            return self._extra[alias]
        row = self._policy.role_for_alias(alias)
        return ModelIdentity(
            alias=row.alias,
            litellm_model=row.litellm_model,
            family=row.family,
            gateway=row.gateway,
            tier=row.tier,
            runs_under_identity=row.runs_under_identity,
        )

    async def record_mapping(
        self,
        alias: str,
        litellm_model: str,
        family: str,
        gateway: str,
        tier: str = "unspecified",
    ) -> None:
        self._extra[alias] = ModelIdentity(
            alias=alias, litellm_model=litellm_model, family=family, gateway=gateway, tier=tier
        )


async def seed_protected_identity(
    store: ProtectedIdentityStore, policy: ModelPolicy | None = None
) -> list[str]:
    """Write every mapped alias into the protected store.

    Called once against WS-B's TerminusDB adapter so the owner audit path
    (GATE-D1-06 A4) has something to reveal. Returns the aliases recorded.
    """
    policy = policy or load_model_policy()
    recorded: list[str] = []
    for row in policy.roles.values():
        await store.record_mapping(
            alias=row.alias,
            litellm_model=row.litellm_model,
            family=row.family,
            gateway=row.gateway,
            tier=row.tier,
        )
        recorded.append(row.alias)
    return recorded


# ---------------------------------------------------------------------------
# Payload scanning -- GATE-D1-06 A1, A2, A5
# ---------------------------------------------------------------------------
def _walk(payload: Any, path: str = "$") -> Iterable[tuple[str, str | None, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield f"{path}.{key}", str(key), value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield f"{path}[{index}]", None, value
            yield from _walk(value, f"{path}[{index}]")


def _banned_tokens(policy: ModelPolicy) -> frozenset[str]:
    """Whole words that identify a vendor or family.

    Deliberately *not* every fragment of every model id: ``qwen-3.6-max`` and
    ``kimi-k2.7-code`` would contribute "max" and "code", and a software
    engineering harness sends the word "code" in almost every instruction. A
    scanner that cries wolf gets switched off, and then GATE-D1-06 is decorative.
    Whole model identifiers are matched separately, as substrings.
    """
    return VENDOR_TOKENS | frozenset(row.family.lower() for row in policy.roles.values())


def _banned_substrings(policy: ModelPolicy) -> frozenset[str]:
    """Complete real model identifiers, matched literally."""
    values = {row.litellm_model.lower().strip() for row in policy.roles.values()}
    for pattern, _reason in policy.prohibited_patterns:
        cleaned = pattern.lower().strip()
        if "*" not in cleaned:
            values.add(cleaned)
    values.update(m.lower() for m in policy.degraded_models)
    return frozenset(v for v in values if len(v) > 3)


def scan_task_payload(payload: Any, policy: ModelPolicy | None = None) -> list[str]:
    """Return every GATE-D1-06 finding in a task-facing payload.

    A finding is a forbidden field name (identity, prestige, or cost tier), a
    vendor/family word, or a complete real model identifier -- anywhere in the
    payload, in keys or values, at any depth.
    """
    policy = policy or load_model_policy()
    tokens = _banned_tokens(policy)
    substrings = _banned_substrings(policy)
    findings: list[str] = []

    def _inspect(text: str, location: str, where: str) -> None:
        lowered = text.lower()
        hits = sorted(set(_TOKEN_RE.findall(lowered)) & tokens)
        hits += sorted(s for s in substrings if s in lowered)
        if hits:
            findings.append(f"{location}: vendor/model identity in {where}: {sorted(set(hits))}")

    for location, key, value in _walk(payload):
        if key is not None:
            if key.lower() in FORBIDDEN_PAYLOAD_KEYS:
                findings.append(f"{location}: forbidden field {key!r} (identity or cost tier)")
            _inspect(key, location, "field name")
        if isinstance(value, str):
            _inspect(value, location, "value")
    return findings


def assert_task_payload_blinded(payload: Any, policy: ModelPolicy | None = None) -> None:
    """GATE-D1-06 gate. Raises ``ROLE_CONFLICT`` on any finding."""
    findings = scan_task_payload(payload, policy)
    if findings:
        raise BlindingViolationError(
            "task-facing payload is not blinded", detail=findings
        )
