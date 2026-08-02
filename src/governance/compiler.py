"""Deterministic emitter for compiled objects.

Contract EFAH-CONTRACT-001 v1.1 Section 8: *every* compiled object carries the
envelope and is content-hashed. This module is the single place compiled objects
are minted so the invariants hold by construction rather than by review:

* the envelope's ``contract_version`` is the governing version and nothing else
  (a v1.0-stamped object after AMENDMENT-001 is ``STALE_CONTRACT_VERSION``);
* the content hash binds envelope *and* body, so a body edited after emission
  fails :meth:`CompiledObject.is_intact`;
* the creating alias is recorded. The contract compiler is deterministic code,
  not a model, so the alias names the mechanism, not a vendor.

No model call occurs anywhere in this path. That is load-bearing: GATE-D1-03's
oracle type is ``deterministic_execution_or_state`` and Section 17.4 forbids a
judge in a deterministic verdict path.
"""

from __future__ import annotations

from typing import Any

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION, CompiledObject
from governance.states import ProjectState

#: Section 12.3 blinded alias. Deterministic compiler, no vendor behind it.
COMPILER_ALIAS = "contract-compiler-d01"


class CompilationError(RuntimeError):
    """A compiled object violates a Section 8 invariant.

    Carries a terminal project state so the caller never has to invent one.
    """

    def __init__(self, message: str, state: ProjectState = ProjectState.FAILED_CONTRACT) -> None:
        super().__init__(message)
        self.state = state


def emit(
    schema_id: str,
    body: dict[str, Any],
    *,
    schema_version: str = "1.0",
    created_by_alias: str = COMPILER_ALIAS,
    terminus_database: str | None = None,
    terminus_branch: str | None = None,
    terminus_commit: str | None = None,
) -> CompiledObject:
    """Mint one sealed compiled object, or raise :class:`CompilationError`."""
    if not schema_id:
        raise CompilationError("compiled object emitted without a schema_id")
    obj = CompiledObject.create(
        schema_id=schema_id,
        schema_version=schema_version,
        created_by_alias=created_by_alias,
        body=body,
        terminus_database=terminus_database,
        terminus_branch=terminus_branch,
        terminus_commit=terminus_commit,
    )
    verify(obj)
    return obj


def verify(obj: CompiledObject) -> None:
    """Assert the Section 8 envelope invariants on an already-minted object."""
    env = obj.envelope
    if env.contract_id != CONTRACT_ID:
        raise CompilationError(f"{env.schema_id}: contract_id {env.contract_id!r} != {CONTRACT_ID!r}")
    if env.contract_version != CONTRACT_VERSION:
        raise CompilationError(
            f"{env.schema_id}: contract_version {env.contract_version!r} != governing {CONTRACT_VERSION!r}"
        )
    if not env.content_hash:
        raise CompilationError(f"{env.schema_id}: emitted without a content hash")
    if not obj.is_intact():
        raise CompilationError(f"{env.schema_id}: content hash does not bind its body")


def emit_all(schema_id: str, bodies: list[dict[str, Any]], **kwargs: Any) -> list[CompiledObject]:
    return [emit(schema_id, body, **kwargs) for body in bodies]
