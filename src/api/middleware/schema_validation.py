"""Schema validation at the boundary (contract Sections 8.1, 11.4).

Pydantic validates *fields* at the route. This middleware validates the
*envelope* before the route is reached: the media type is what we accept, the
body is well-formed JSON, and the top level is an object rather than a bare
scalar. Failing here yields one typed ``SCHEMA_VALIDATION_FAILED`` instead of a
framework 400 with a body shape nothing downstream can classify.
"""

from __future__ import annotations

import json
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.errors import SchemaValidationFailed
from api.middleware.limits import captured_body

ACCEPTED_MEDIA_TYPES: Final = frozenset({"application/json", "application/json; charset=utf-8"})
_BODY_METHODS: Final = frozenset({"POST", "PUT", "PATCH"})


class SchemaValidationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _BODY_METHODS:
            return await call_next(request)

        body = captured_body(request.scope)
        if not body:
            # An empty body is legitimate for commands whose fields all default.
            return await call_next(request)

        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type and media_type != "application/json":
            raise SchemaValidationFailed(
                f"unsupported media type {media_type!r}; this API accepts application/json"
            )

        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SchemaValidationFailed(f"request body is not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise SchemaValidationFailed(
                "request body must be a JSON object; a bare array or scalar cannot carry "
                "the named fields a command requires (Section 8.1)"
            )
        return await call_next(request)
