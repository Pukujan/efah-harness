"""Typed API failures (contract Sections 6.2, 10.6, 10.7, 19.2).

Non-negotiable: an error leaving this API carries a state string that already
exists in ``governance.states``. Inventing ``{"error": "bad request"}`` would
give the dashboard and the drift engine a category they cannot act on.

Every error body carries the contract binding and the correlation ids, so a
failure seen by the owner in Plane can be joined to the exact trace in Phoenix.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from api.context import current_context
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import DriftFinding, FailureClass, OwnerInterrupt, ProjectState, TaskState


class ApiError(Exception):
    """Base for every failure this API is willing to return.

    ``code`` is always a value from a closed governance enumeration.
    """

    status_code: int = 500
    code: str = "FAILED_INFRASTRUCTURE"

    def __init__(self, detail: str, **extra: Any) -> None:
        self.detail = detail
        self.extra = extra
        super().__init__(detail)

    def body(self) -> dict[str, Any]:
        context = current_context()
        payload: dict[str, Any] = {
            "code": self.code,
            "detail": self.detail,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
        }
        if context is not None:
            payload["correlation_id"] = context.correlation_id
            payload["request_id"] = context.request_id
            if context.trace_id:
                payload["trace_id"] = context.trace_id
        payload.update(self.extra)
        return {"error": payload}


class Unauthenticated(ApiError):
    """No usable credential. Section 11.4 authentication."""

    status_code = 401
    code = "UNAUTHENTICATED"


class Unauthorized(ApiError):
    """Authenticated but out of scope. Section 11.4 authorization."""

    status_code = 403
    code = "UNAUTHORIZED"


class StaleContractVersion(ApiError):
    """Section 11.4 contract/project version binding; Section 19.2 drift type.

    409 rather than 400: the request is well formed, it is bound to a contract
    revision this harness no longer governs. The caller must re-read the
    contract, not fix a field.
    """

    status_code = 409
    code = DriftFinding.STALE_CONTRACT_VERSION

    def __init__(self, declared: str, expected: str) -> None:
        super().__init__(
            f"request is bound to contract version {declared!r}; this harness governs "
            f"{expected!r}. Re-read the contract before retrying.",
            declared_contract_version=declared,
            expected_contract_version=expected,
        )


class SchemaValidationFailed(ApiError):
    """Section 11.4 schema validation. Section 8.1 forbids silent defaults."""

    status_code = 422
    code = "SCHEMA_VALIDATION_FAILED"


class PackValidationRejected(ApiError):
    """The project pack did not validate (Sections 6.1, 8.1).

    ``contract.yaml`` phase gate ``project_pack_validation`` fails to a typed
    ``missing_or_invalid_input`` blocker, which is a contract failure, not a
    scope one -- the caller supplied an input the contract cannot accept.
    Nothing is imported: Section 8.1 forbids substituting a default for a
    material field, so a partially-valid pack is rejected whole.
    """

    status_code = 422
    code = ProjectState.FAILED_CONTRACT


class InputLimitExceeded(ApiError):
    """Section 11.4 input limits."""

    status_code = 413
    code = "INPUT_LIMIT_EXCEEDED"


class RateLimited(ApiError):
    """Section 11.4 rate and concurrency controls."""

    status_code = 429
    code = FailureClass.RATE_LIMIT


class ConcurrencyLimited(ApiError):
    """Section 11.4 concurrency controls. Distinct from a rate limit."""

    status_code = 429
    code = "CONCURRENCY_LIMIT"


class UntrustedContentRejected(ApiError):
    """Section 11.4 prompt-injection and untrusted-content boundary."""

    status_code = 422
    code = DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION


class ScopeExpansionRejected(ApiError):
    """Section 19.2 UNAPPROVED_SCOPE_EXPANSION. Rejected, never executed."""

    status_code = 403
    code = DriftFinding.UNAPPROVED_SCOPE_EXPANSION


class ProtectedAssetAccess(ApiError):
    """Section 17.2 / 11.2. The sealed side and real model identity are closed."""

    status_code = 403
    code = DriftFinding.PROTECTED_ASSET_ACCESS


class NotFound(ApiError):
    """The addressed entity does not exist in authoritative state."""

    status_code = 404
    code = "NOT_FOUND"


class OwnerDecisionRequired(ApiError):
    """A typed owner interrupt blocks the command (Section 10.7).

    424 Failed Dependency: the request is valid and the harness is healthy; it
    is waiting on a human decision that only the owner may supply.
    """

    status_code = 424
    code = OwnerInterrupt.OWNER_SCOPE_DECISION

    def __init__(self, detail: str, interrupt: OwnerInterrupt, **extra: Any) -> None:
        self.code = str(interrupt)
        super().__init__(detail, **extra)


class GateBypassRejected(ApiError):
    """A command tried to reach a gate-only state (Section 9.3)."""

    status_code = 403
    code = TaskState.FAILED_SCOPE


class ProjectionUnavailable(ApiError):
    """Plane is unreachable. Section 4.1 / plane.yaml: this is NOT fatal.

    503 with a typed ``degraded_projection`` body so a caller can distinguish
    "the projection is stale" from "the project failed".
    """

    status_code = 503
    code = "degraded_projection"


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())
