"""Input limits and single-read body capture (contract Section 11.4).

Pure ASGI rather than ``BaseHTTPMiddleware`` for one specific reason: the body
must be read exactly once, size-checked *while* it streams, and then made
available to the four middlewares downstream that need it (schema validation,
untrusted-content scanning, provenance hashing, audit). Reading it four times
would either fail or buffer four copies.

The size check happens per chunk, so a caller cannot bypass it by omitting
``Content-Length`` and streaming an unbounded body.
"""

from __future__ import annotations

from typing import Any, Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.errors import InputLimitExceeded

#: Where the captured body lands for downstream middleware.
BODY_KEY: Final = "efah_request_body"

DEFAULT_MAX_BODY_BYTES: Final = 1_048_576  # 1 MiB
DEFAULT_MAX_HEADER_COUNT: Final = 64
DEFAULT_MAX_URL_LENGTH: Final = 4096


class InputLimitsMiddleware:
    """Rejects oversized requests before any work is done on them."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_header_count: int = DEFAULT_MAX_HEADER_COUNT,
        max_url_length: int = DEFAULT_MAX_URL_LENGTH,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_header_count = max_header_count
        self.max_url_length = max_url_length

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        if len(raw_headers) > self.max_header_count:
            raise InputLimitExceeded(
                f"request carries {len(raw_headers)} headers; the limit is {self.max_header_count}"
            )

        url_length = len(scope.get("raw_path") or scope.get("path", "").encode()) + len(
            scope.get("query_string", b"")
        )
        if url_length > self.max_url_length:
            raise InputLimitExceeded(
                f"request URL is {url_length} bytes; the limit is {self.max_url_length}"
            )

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            raise InputLimitExceeded(
                f"declared body of {declared} bytes exceeds the {self.max_body_bytes}-byte limit"
            )

        body = bytearray()
        more = True
        while more:
            message: Message = await receive()
            if message["type"] == "http.disconnect":
                more = False
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                raise InputLimitExceeded(
                    f"request body exceeds the {self.max_body_bytes}-byte limit"
                )
            more = message.get("more_body", False)

        captured = bytes(body)
        scope[BODY_KEY] = captured

        async def replay() -> Message:
            return {"type": "http.request", "body": captured, "more_body": False}

        await self.app(scope, replay, send)


def captured_body(scope: dict[str, Any]) -> bytes:
    """The body :class:`InputLimitsMiddleware` already read, or ``b''``."""
    return scope.get(BODY_KEY, b"")
