"""Request provenance (contract Sections 11.4, 18).

Section 18 binds every result to the artifact that produced it. For an API that
means the accepted request body is content-hashed and recorded *as accepted*, so
a later "the harness did X" can be answered with the exact bytes that asked for
it -- not a re-serialisation, not a summary.

The hash uses :func:`governance.envelope.content_hash`, the same function that
seals compiled objects, so a request hash and an artifact hash are comparable.
Only the hash is recorded, never the body: a body may hold owner prose, and the
provenance record is written to logs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import current_context
from api.middleware.limits import captured_body
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION, content_hash


class ProvenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        context = current_context()
        if context is not None:
            body = captured_body(request.scope)
            context.provenance.update(
                {
                    "received_at": datetime.now(UTC).isoformat(),
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "client_host": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                    "body_bytes": len(body),
                    "body_content_hash": content_hash(body) if body else None,
                    "contract_id": CONTRACT_ID,
                    "contract_version": CONTRACT_VERSION,
                    "correlation_id": context.correlation_id,
                    "request_id": context.request_id,
                    "trace_id": context.trace_id,
                    **context.principal.audit_identity(),
                }
            )
        return await call_next(request)
