"""Envelope sealing and verification for control-plane entities.

Contract Section 8 puts eleven header fields on every compiled object; Section
15.2 says every *material* write creates an attributable immutable commit. Those
two meet here: ``terminus_commit`` cannot be known until the commit exists, so
the write is two-phase (see :mod:`provenance.writer`) and this module owns the
sealing arithmetic for both phases.

``content_hash`` covers the envelope-minus-its-own-hash plus the entity body
with the envelope removed. Both exclusions are necessary: without the first the
hash would depend on itself, without the second re-sealing after the commit id
is known would hash a stale copy of the previous envelope.
"""

from __future__ import annotations

from typing import Any, TypeVar

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION, Envelope
from governance.states import DriftFinding
from ontology.schema import ControlPlaneEntity

__all__ = [
    "REQUIRED_ENVELOPE_FIELDS",
    "MissingProvenanceBinding",
    "StaleContractVersion",
    "entity_body",
    "require_current_contract",
    "seal_entity",
    "verify_entity",
]

E = TypeVar("E", bound=ControlPlaneEntity)

#: GATE-D1-02 A1. The gate lists ``schema_id`` through ``created_at``; all
#: eleven must be present on a persisted object.
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "schema_id",
    "schema_version",
    "contract_id",
    "contract_version",
    "methodology_version",
    "terminus_database",
    "terminus_branch",
    "terminus_commit",
    "content_hash",
    "created_by_alias",
    "created_at",
)


class StaleContractVersion(RuntimeError):
    """Drift finding ``STALE_CONTRACT_VERSION`` (Section 19.2).

    Raised, never migrated silently -- GATE-D1-02 A2 injects a stale version as a
    negative control and expects rejection.
    """

    finding = DriftFinding.STALE_CONTRACT_VERSION


class MissingProvenanceBinding(RuntimeError):
    """An entity reached persistence without database/branch/commit binding."""


def entity_body(entity: ControlPlaneEntity) -> dict[str, Any]:
    """The hashable body: everything except the envelope, canonicalised."""
    return entity.model_dump(mode="json", exclude={"envelope"})


def seal_entity(
    entity: E,
    *,
    database: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
) -> E:
    """Return a copy of *entity* whose envelope is bound and hashed.

    Any of *database*, *branch*, *commit* left as ``None`` keeps the value the
    envelope already carries, so phase two can add only the commit id.
    """
    env = entity.envelope
    updated = env.model_copy(
        update={
            "terminus_database": database if database is not None else env.terminus_database,
            "terminus_branch": branch if branch is not None else env.terminus_branch,
            "terminus_commit": commit if commit is not None else env.terminus_commit,
            "content_hash": None,
        }
    )
    sealed_entity = entity.model_copy(update={"envelope": updated})
    body = entity_body(sealed_entity)
    return entity.model_copy(update={"envelope": updated.sealed(body)})


def verify_entity(entity: ControlPlaneEntity) -> bool:
    """True when the stored ``content_hash`` still matches envelope + body."""
    return entity.envelope.verify(entity_body(entity))


def require_current_contract(envelope: Envelope) -> None:
    """Reject an object bound to a different contract or a stale version.

    v1.1 is v1.0 plus an additive amendment, so an object recorded against v1.0
    remains valid; anything *older* than v1.0, or bound to another contract
    entirely, is stale and must not be silently migrated.
    """
    if envelope.contract_id != CONTRACT_ID:
        raise StaleContractVersion(
            f"object is bound to contract {envelope.contract_id!r}, expected {CONTRACT_ID!r}"
        )
    if _version_tuple(envelope.contract_version) > _version_tuple(CONTRACT_VERSION):
        raise StaleContractVersion(
            f"object declares contract_version {envelope.contract_version!r}, "
            f"which is ahead of the governing {CONTRACT_VERSION!r}"
        )
    if _version_tuple(envelope.contract_version) < (1, 0):
        raise StaleContractVersion(
            f"object declares stale contract_version {envelope.contract_version!r}"
        )


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def assert_fully_bound(entity: ControlPlaneEntity) -> None:
    """GATE-D1-02 A1: every envelope field present, none defaulted to blank."""
    env = entity.envelope
    missing = [name for name in REQUIRED_ENVELOPE_FIELDS if getattr(env, name, None) in (None, "")]
    if missing:
        raise MissingProvenanceBinding(
            f"{entity.document_id} is missing envelope fields: {missing}"
        )
