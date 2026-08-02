"""Protected model-identity store (contract Section 11.2).

Section 11.1 says other agents "never receive the real vendor/model identity".
Section 11.2 says the mapping lives in a separate protected database, and that
the *owner* -- not a worker, not a gate -- may reveal it later for audit.

This module is the whole of the harness's route to the protected instance at
``http://localhost:6364``. That is not a convention: ``tests/architecture`` and
``tests/unit/test_terminus_protected_isolation.py`` fail the build if any other
module mentions the port or the protected credential. GATE-D1-08's remediation
clause is explicit that the gate must not be made to pass by granting access, so
the isolation is asserted rather than arranged.

Two surfaces, deliberately asymmetric:

* :meth:`ProtectedIdentityStore.alias_view` -- task-facing. Alias, role, gateway,
  and capability flags. No provider, no model id. Returning a
  :class:`ProtectedModelIdentity` here would be the leak the gate looks for.
* :meth:`ProtectedIdentityStore.reveal_for_owner_audit` -- owner-facing. Requires
  an :class:`OwnerAuditRequest` naming the owner and the reason, and writes an
  immutable audit record *before* returning the identity, so a reveal cannot
  happen without leaving a trace.

Measured isolation, 2026-08-02 against the running pair:

===========================  ===================  ======
Credential                   Endpoint             Result
===========================  ===================  ======
``TERMINUSDB_ADMIN_PASS``    ``localhost:6364``   401
``TERMINUSDB_PROTECTED_PASS`` ``localhost:6364``  200
``TERMINUSDB_PROTECTED_PASS`` ``localhost:6363``  401
===========================  ===================  ======
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from governance.envelope import content_hash
from integrations.secrets import SecretRef, SecretResolver
from integrations.terminusdb import (
    TerminusAuthError,
    TerminusClient,
    TerminusConfig,
    TerminusError,
)

__all__ = [
    "PROTECTED_ENDPOINT",
    "PROTECTED_DATABASE",
    "PROTECTED_PASSWORD_REF",
    "ProtectedIdentityStore",
    "ProtectedIdentityAccessError",
    "ProtectedModelIdentity",
    "AliasView",
    "OwnerAuditRequest",
    "IsolationProbeResult",
    "probe_credential_against_protected",
    "protected_store_from_env",
]

PROTECTED_ENDPOINT = "http://localhost:6364"
PROTECTED_DATABASE = "efah_protected"
PROTECTED_PASSWORD_REF = "env:TERMINUSDB_PROTECTED_PASS"

#: Contract Section 11.2 -- the protected credential is withheld from every task
#: participant (``secrets.refs.yaml`` ``withheld_from``). Only this alias may
#: resolve it.
PROTECTED_CREDENTIAL_ALIAS = "terminusdb_protected_auth"

_IDENTITY_CLASS = "ProtectedModelIdentity"
_AUDIT_CLASS = "IdentityRevealAudit"

#: JSON-LD schema for the protected database. Deliberately tiny: the protected
#: instance holds the mapping and its audit trail and nothing else, so a
#: compromise of it discloses no project content.
PROTECTED_SCHEMA: list[dict[str, Any]] = [
    {
        "@type": "Class",
        "@id": _IDENTITY_CLASS,
        "@key": {"@type": "Lexical", "@fields": ["alias"]},
        "alias": "xsd:string",
        "provider": "xsd:string",
        "model_id": "xsd:string",
        "gateway": "xsd:string",
        "configuration_hash": "xsd:string",
        "recorded_at": "xsd:dateTime",
        "role": {"@type": "Optional", "@class": "xsd:string"},
        "gate_bearing": {"@type": "Optional", "@class": "xsd:boolean"},
    },
    {
        "@type": "Class",
        "@id": _AUDIT_CLASS,
        "@key": {"@type": "Lexical", "@fields": ["audit_id"]},
        "audit_id": "xsd:string",
        "alias": "xsd:string",
        "owner_identity": "xsd:string",
        "reason": "xsd:string",
        "revealed_at": "xsd:dateTime",
    },
]


class ProtectedIdentityAccessError(RuntimeError):
    """An attempt to reach the real identity without an owner audit context.

    Maps to drift finding ``PROTECTED_ASSET_ACCESS`` (contract Section 19.2).
    """


@dataclass(frozen=True)
class ProtectedModelIdentity:
    """The real vendor/model behind an alias. Never crosses to the task side."""

    alias: str
    provider: str
    model_id: str
    gateway: str
    configuration_hash: str
    role: str | None = None
    gate_bearing: bool = False
    recorded_at: str | None = None


@dataclass(frozen=True)
class AliasView:
    """What a task participant is permitted to see (Section 11.1).

    There is no ``provider`` and no ``model_id`` field, so a caller cannot leak
    one by accident -- the object simply does not carry it.
    """

    alias: str
    role: str | None
    gateway: str
    gate_bearing: bool


@dataclass(frozen=True)
class OwnerAuditRequest:
    """Owner-supplied justification for revealing a mapping."""

    owner_identity: str
    reason: str

    def __post_init__(self) -> None:
        if not self.owner_identity.strip():
            raise ProtectedIdentityAccessError("owner_identity is required to reveal a protected identity")
        if not self.reason.strip():
            raise ProtectedIdentityAccessError("a reason is required to reveal a protected identity")


@dataclass(frozen=True)
class IsolationProbeResult:
    """Evidence for GATE-D1-08 A1: a request transcript with the actor identity."""

    endpoint: str
    actor: str
    status: int
    api_error_type: str | None
    probed_at: str

    @property
    def is_denied(self) -> bool:
        return self.status in (401, 403, 404)


async def probe_credential_against_protected(
    password: str,
    *,
    actor: str,
    user: str = "admin",
    endpoint: str = PROTECTED_ENDPOINT,
    timeout: float = 10.0,
) -> IsolationProbeResult:
    """Ask the protected instance whether *password* is accepted.

    Used to prove the main builder credential is refused. It returns a result
    rather than raising because a 401 here is the *expected* outcome and a
    passing observation, not an error.
    """
    async with httpx.AsyncClient(base_url=endpoint.rstrip("/"), auth=(user, password), timeout=timeout) as c:
        response = await c.get("/api/info")
    api_error_type = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("api:error")
        if isinstance(err, dict):
            api_error_type = err.get("@type")
    return IsolationProbeResult(
        endpoint=endpoint,
        actor=actor,
        status=response.status_code,
        api_error_type=api_error_type,
        probed_at=datetime.now(UTC).isoformat(),
    )


class ProtectedIdentityStore:
    """Alias -> real model identity, held on the isolated instance.

    The client is private. There is no accessor that hands it out, because a
    caller holding the client holds the credential's reach.
    """

    def __init__(
        self,
        *,
        password: str,
        endpoint: str = PROTECTED_ENDPOINT,
        database: str = PROTECTED_DATABASE,
        user: str = "admin",
        org: str = "admin",
        branch: str = "main",
    ) -> None:
        self._database = database
        self._branch = branch
        self._endpoint = endpoint.rstrip("/")
        self.__client = TerminusClient(
            TerminusConfig(endpoint=self._endpoint, user=user, password=password, org=org)
        )

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def database(self) -> str:
        return self._database

    async def __aenter__(self) -> "ProtectedIdentityStore":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.__client.aclose()

    async def ensure_ready(self) -> bool:
        """Create the protected database and its schema if absent.

        Returns True when this call created the database.
        """
        created = await self.__client.ensure_database(
            self._database,
            label="EFAH protected model identity",
            comment="Contract Section 11.2: alias -> real model identity. Owner audit only.",
        )
        existing = {
            d.get("@id") for d in await self.__client.get_documents(
                self._database, branch=self._branch, graph_type="schema"
            )
        }
        missing = [doc for doc in PROTECTED_SCHEMA if doc["@id"] not in existing]
        if missing:
            await self.__client.insert_documents(
                self._database,
                missing,
                author=PROTECTED_CREDENTIAL_ALIAS,
                message="protected identity schema",
                branch=self._branch,
                graph_type="schema",
            )
        return created

    async def record_mapping(self, identity: ProtectedModelIdentity) -> str:
        """Write (or overwrite) one alias -> identity mapping."""
        doc = {
            "@type": _IDENTITY_CLASS,
            "@id": f"{_IDENTITY_CLASS}/{identity.alias}",
            "alias": identity.alias,
            "provider": identity.provider,
            "model_id": identity.model_id,
            "gateway": identity.gateway,
            "configuration_hash": identity.configuration_hash,
            "recorded_at": identity.recorded_at or datetime.now(UTC).isoformat(),
            "gate_bearing": identity.gate_bearing,
        }
        if identity.role is not None:
            doc["role"] = identity.role
        ids = await self.__client.replace_documents(
            self._database,
            [doc],
            author=PROTECTED_CREDENTIAL_ALIAS,
            message=f"record protected identity for alias {identity.alias}",
            branch=self._branch,
            create=True,
        )
        return ids[0] if ids else doc["@id"]

    async def seed_from_model_policy(self, model_policy: dict[str, Any]) -> list[str]:
        """Move the real identities out of ``model-policy.yaml`` and in here.

        ``model-policy.yaml`` ships the real ``family`` and ``litellm_model`` for
        every alias. The pack importer deliberately drops both when writing to
        the main graph; this is where they go instead. Returns the aliases stored.
        """
        aliases = model_policy.get("aliases") or {}
        gate_bearing_roles = set(
            ((model_policy.get("gateway_routing") or {}).get("eval") or {}).get("permitted_roles") or []
        )
        stored: list[str] = []
        for role, block in aliases.items():
            if not isinstance(block, dict) or "alias" not in block:
                continue
            identity = ProtectedModelIdentity(
                alias=str(block["alias"]),
                provider=str(block.get("family", "unknown")),
                model_id=str(block.get("litellm_model", "unknown")),
                gateway=str(block.get("gateway", "production")),
                configuration_hash=content_hash(block),
                role=str(role),
                gate_bearing=str(role) in gate_bearing_roles,
            )
            await self.record_mapping(identity)
            stored.append(identity.alias)
        return stored

    async def known_aliases(self) -> list[str]:
        """Task-safe: alias strings only."""
        docs = await self.__client.get_documents(
            self._database, branch=self._branch, doc_type=_IDENTITY_CLASS
        )
        return sorted(str(d["alias"]) for d in docs if "alias" in d)

    async def alias_view(self, alias: str) -> AliasView | None:
        """Task-facing lookup. Cannot return provider or model id -- by type."""
        doc = await self._raw_identity(alias)
        if doc is None:
            return None
        return AliasView(
            alias=str(doc["alias"]),
            role=doc.get("role"),
            gateway=str(doc["gateway"]),
            gate_bearing=bool(doc.get("gate_bearing", False)),
        )

    async def reveal_for_owner_audit(
        self, alias: str, request: OwnerAuditRequest
    ) -> ProtectedModelIdentity | None:
        """Owner-only reveal. Writes the audit record before returning.

        Ordering is the point: if the audit write fails the identity is not
        returned, so there is no reveal without a trace.
        """
        if not isinstance(request, OwnerAuditRequest):  # defensive: no duck-typed bypass
            raise ProtectedIdentityAccessError("reveal requires an OwnerAuditRequest")
        doc = await self._raw_identity(alias)
        if doc is None:
            return None
        revealed_at = datetime.now(UTC).isoformat()
        audit_id = content_hash(
            {
                "alias": alias,
                "owner": request.owner_identity,
                "reason": request.reason,
                "at": revealed_at,
            }
        ).removeprefix("sha256:")
        await self.__client.insert_documents(
            self._database,
            [
                {
                    "@type": _AUDIT_CLASS,
                    "@id": f"{_AUDIT_CLASS}/{audit_id}",
                    "audit_id": audit_id,
                    "alias": alias,
                    "owner_identity": request.owner_identity,
                    "reason": request.reason,
                    "revealed_at": revealed_at,
                }
            ],
            author=request.owner_identity,
            message=f"owner audit reveal of alias {alias}: {request.reason}",
            branch=self._branch,
        )
        return ProtectedModelIdentity(
            alias=str(doc["alias"]),
            provider=str(doc["provider"]),
            model_id=str(doc["model_id"]),
            gateway=str(doc["gateway"]),
            configuration_hash=str(doc["configuration_hash"]),
            role=doc.get("role"),
            gate_bearing=bool(doc.get("gate_bearing", False)),
            recorded_at=doc.get("recorded_at"),
        )

    async def audit_trail(self, alias: str | None = None) -> list[dict[str, Any]]:
        docs = await self.__client.get_documents(
            self._database, branch=self._branch, doc_type=_AUDIT_CLASS
        )
        if alias is not None:
            docs = [d for d in docs if d.get("alias") == alias]
        return sorted(docs, key=lambda d: str(d.get("revealed_at", "")))

    async def assert_credential_is_isolated(self, other_password: str, *, actor: str) -> IsolationProbeResult:
        """Prove *other_password* cannot reach this instance (GATE-D1-08 A1).

        Raises rather than returns when the other credential is accepted: a 200
        is a hard failure, and this method exists to make that impossible to
        overlook.
        """
        result = await probe_credential_against_protected(
            other_password, actor=actor, endpoint=self._endpoint
        )
        if not result.is_denied:
            raise ProtectedIdentityAccessError(
                f"PROTECTED_ASSET_ACCESS: {actor} reached {self._endpoint} with HTTP {result.status}"
            )
        return result

    async def _raw_identity(self, alias: str) -> dict[str, Any] | None:
        try:
            return await self.__client.get_document(
                self._database, f"{_IDENTITY_CLASS}/{alias}", branch=self._branch
            )
        except TerminusAuthError:
            raise
        except TerminusError:
            return None


def protected_store_from_env(
    *,
    endpoint: str = PROTECTED_ENDPOINT,
    database: str = PROTECTED_DATABASE,
    environ: dict[str, str] | None = None,
) -> ProtectedIdentityStore:
    """Build the store from the withheld credential reference.

    Raises ``MissingRequiredCredential`` when the protected password is absent,
    which is the typed owner interrupt (Section 10.7), not a silent fallback to
    the main credential.
    """
    resolver = SecretResolver(environ)
    password = resolver.resolve(
        SecretRef(name=PROTECTED_CREDENTIAL_ALIAS, reference=PROTECTED_PASSWORD_REF, required=True)
    )
    assert password is not None
    return ProtectedIdentityStore(password=password, endpoint=endpoint, database=database)
