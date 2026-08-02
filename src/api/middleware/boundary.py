"""Typed-error boundary for the middleware stack.

Starlette's exception handlers wrap the *router*, not the middleware chain: an
exception raised inside ``add_middleware`` middleware never reaches
``add_exception_handler`` and surfaces as a bare 500 with no body. That would
turn every authentication, version-binding, input-limit, throttle, and
untrusted-content refusal -- all of which are middleware -- into an
unclassifiable 500, which is precisely what contract Section 6.2's closed state
list exists to prevent.

So the boundary sits just inside the correlation middleware: outside every
middleware that can raise, inside the one that mints the correlation id, so a
refusal still carries the ids needed to trace it.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.errors import ApiError


class TypedErrorBoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except ApiError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.body())
