"""Correlation and trace IDs (contract Sections 11.4, 23).

Every request gets a correlation id (supplied by the caller or minted here), a
per-attempt request id, and a real OpenTelemetry span. The trace id is read back
*off the span* -- never generated separately -- so the id in the response header
is the id that will be in Phoenix, and a support question of the form "here is
my correlation id, what happened?" is answerable.

This middleware is outermost. It is also the only one that may run before the
request context exists, so every other middleware can assume the context is set.
"""

from __future__ import annotations

import uuid
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import RequestContext, reset_context, set_context
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from observability.spans import Correlation, SpanKindName, efah_span, format_trace_id

CORRELATION_HEADER: Final = "x-correlation-id"
REQUEST_ID_HEADER: Final = "x-request-id"
TRACE_ID_HEADER: Final = "x-trace-id"
CONTRACT_HEADER: Final = "x-efah-contract"


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        request_id = uuid.uuid4().hex

        context = RequestContext(
            correlation_id=correlation_id,
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            contract_id=CONTRACT_ID,
            contract_version=CONTRACT_VERSION,
        )
        token = set_context(context)
        try:
            with efah_span(
                f"{request.method} {request.url.path}",
                kind=SpanKindName.API_REQUEST,
                correlation=Correlation(run_id=request_id),
                attributes={
                    "http.request.method": request.method,
                    "url.path": request.url.path,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                },
            ) as span:
                context.trace_id = format_trace_id(span)
                response = await call_next(request)
                span.set_attribute("http.response.status_code", response.status_code)
                # Stamped after the fact: the project id comes from the path or
                # a header that the version-binding middleware resolves *below*
                # this one, and a Section 23 span without it cannot be joined to
                # the project it acted on.
                if context.project_id:
                    span.set_attribute("efah.project_id", context.project_id)
        finally:
            reset_context(token)

        response.headers[CORRELATION_HEADER] = correlation_id
        response.headers[REQUEST_ID_HEADER] = request_id
        if context.trace_id:
            response.headers[TRACE_ID_HEADER] = context.trace_id
        response.headers[CONTRACT_HEADER] = f"{CONTRACT_ID}@{CONTRACT_VERSION}"
        return response
