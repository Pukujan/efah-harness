"""Compiled-object envelope and content hashing.

Contract EFAH-CONTRACT-001 v1.1 Section 8: *every* compiled object MUST carry
the envelope fields below. Section 18: "done" without named evidence is invalid,
and every result carries an evidence/provenance tier.

This module is deliberately dependency-free apart from pydantic so that every
other module -- including the protected-verifier interface -- can import it
without pulling in a transitive dependency on any model vendor.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_ID = "EFAH-CONTRACT-001"
CONTRACT_VERSION = "1.1"
METHODOLOGY_VERSION = "1.0.0"


class EvidenceTier(StrEnum):
    """Contract Section 18. Ordered strongest to weakest."""

    OWNER_VERIFIED = "OWNER_VERIFIED"
    DETERMINISTIC_ORACLE = "DETERMINISTIC_ORACLE"
    INDEPENDENTLY_REPRODUCED = "INDEPENDENTLY_REPRODUCED"
    CALIBRATED_MODEL_VERIFIED = "CALIBRATED_MODEL_VERIFIED"
    AI_DISCOVERED_UNVERIFIED = "AI_DISCOVERED_UNVERIFIED"


class KnowledgeTier(StrEnum):
    """Contract Section 15.5. Unverified agent output must not be trusted."""

    T0_RAW = "T0_RAW"
    T1_OBSERVATION = "T1_OBSERVATION"
    T2_HYPOTHESIS = "T2_HYPOTHESIS"
    T3_TESTED = "T3_TESTED"
    T4_REPRODUCIBLE = "T4_REPRODUCIBLE"
    T5_INDEPENDENTLY_VERIFIED = "T5_INDEPENDENTLY_VERIFIED"
    T6_APPROVED_OPERATIONAL_KNOWLEDGE = "T6_APPROVED_OPERATIONAL_KNOWLEDGE"
    T7_HARD_GOLD = "T7_HARD_GOLD"


#: Below this tier, knowledge may not be presented as trusted (Section 15.5).
TRUSTED_KNOWLEDGE_FLOOR = KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE


def canonical_json(payload: Any) -> str:
    """Stable serialisation so a content hash is reproducible across hosts.

    Sorted keys and no insignificant whitespace: two structurally equal objects
    must hash equal, or Section 18's artifact binding is unenforceable.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(payload: Any) -> str:
    """Return the ``sha256:...`` content hash used throughout the contract."""
    if isinstance(payload, bytes):
        digest = hashlib.sha256(payload).hexdigest()
    elif isinstance(payload, str):
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    else:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Envelope(BaseModel):
    """Contract Section 8 required fields on every compiled object.

    ``content_hash`` is excluded from its own computation -- see
    :meth:`sealed`. Without that exclusion the hash would depend on itself and
    could never be verified.
    """

    model_config = ConfigDict(extra="forbid")

    schema_id: str
    schema_version: str = "1.0"
    contract_id: str = CONTRACT_ID
    contract_version: str = CONTRACT_VERSION
    methodology_version: str = METHODOLOGY_VERSION
    terminus_database: str | None = None
    terminus_branch: str | None = None
    terminus_commit: str | None = None
    content_hash: str | None = None
    created_by_alias: str
    created_at: str = Field(default_factory=utc_now)

    def sealed(self, body: Any) -> Envelope:
        """Return a copy whose ``content_hash`` binds envelope *and* body."""
        base = self.model_dump(exclude={"content_hash"})
        return self.model_copy(update={"content_hash": content_hash({"envelope": base, "body": body})})

    def verify(self, body: Any) -> bool:
        if self.content_hash is None:
            return False
        return self.sealed(body).content_hash == self.content_hash


class CompiledObject(BaseModel):
    """Envelope + body. The unit the mechanical verifier checks (Section 18)."""

    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    body: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        schema_id: str,
        created_by_alias: str,
        body: dict[str, Any],
        schema_version: str = "1.0",
        terminus_database: str | None = None,
        terminus_branch: str | None = None,
        terminus_commit: str | None = None,
    ) -> CompiledObject:
        env = Envelope(
            schema_id=schema_id,
            schema_version=schema_version,
            created_by_alias=created_by_alias,
            terminus_database=terminus_database,
            terminus_branch=terminus_branch,
            terminus_commit=terminus_commit,
        )
        return cls(envelope=env.sealed(body), body=body)

    def is_intact(self) -> bool:
        return self.envelope.verify(self.body)
