"""Audit logging (contract Sections 11.4, 18).

One structured record per request, emitted after the outcome is known, holding
identity, provenance, contract binding, correlation ids, status, and duration.

Two properties matter more than the format:

* **Failures are audited too.** The record is written in a ``finally``, so a
  request that raised is logged with its typed error class. An audit log that
  only contains successes is a marketing artifact.
* **No secrets, no bodies.** The record carries the body's content hash from the
  provenance middleware, never the body, and never an ``Authorization`` header.

Records are pushed to an in-process sink as well as the logger so a test can
assert on them without parsing log text.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any, Deque, Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import current_context
from api.errors import ApiError

LOGGER: Final = logging.getLogger("efah.audit")

#: Never audited, at any level.
REDACTED_HEADERS: Final = frozenset({"authorization", "cookie", "x-api-key", "proxy-authorization"})


class AuditSink:
    """Bounded in-memory ring of the most recent audit records."""

    def __init__(self, capacity: int = 1000) -> None:
        self._records: Deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, record: dict[str, Any]) -> None:
        self._records.append(record)

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, sink: AuditSink | None = None) -> None:  # noqa: ANN001
        super().__init__(app)
        self.sink = sink or AuditSink()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        status_code: int | None = None
        error_code: str | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except ApiError as exc:
            status_code = exc.status_code
            error_code = exc.code
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised after auditing
            status_code = 500
            error_code = type(exc).__name__
            raise
        finally:
            context = current_context()
            record: dict[str, Any] = {
                "event": "api_request",
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_code": error_code,
                "headers_present": sorted(
                    name for name in request.headers.keys() if name.lower() not in REDACTED_HEADERS
                ),
            }
            if context is not None:
                record.update(
                    {
                        "correlation_id": context.correlation_id,
                        "request_id": context.request_id,
                        "trace_id": context.trace_id,
                        "contract_id": context.contract_id,
                        "contract_version": context.contract_version,
                        "project_id": context.project_id,
                        "provenance": context.provenance,
                        **context.principal.audit_identity(),
                    }
                )
            self.sink.append(record)
            LOGGER.info("%s", json.dumps(record, default=str, sort_keys=True))
