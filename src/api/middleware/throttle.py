"""Rate and concurrency controls (contract Section 11.4).

Two distinct limits, because they fail differently:

* **rate** -- requests per window per principal. Protects the control plane from
  a runaway caller.
* **concurrency** -- simultaneous in-flight requests per principal. Protects the
  *downstream* from fan-out: a hundred concurrent ``/projects/{id}/run`` calls
  would each want a model round and blow the account-wide 90 req/min cap that
  ``model-policy.yaml`` shares across the whole fleet.

Collapsing them into one number would let a caller that respects the rate limit
still open unbounded concurrent work.

Fixed-window rather than token-bucket: a single-host deadline build does not
need burst smoothing, and a simpler limiter is a limiter whose behaviour under
load is predictable.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import current_context
from api.errors import ConcurrencyLimited, RateLimited

DEFAULT_RATE_LIMIT: Final = 120
DEFAULT_WINDOW_SECONDS: Final = 60.0
DEFAULT_MAX_CONCURRENT: Final = 16


class ThrottleMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        exempt_paths: frozenset[str] = frozenset({"/health"}),
    ) -> None:
        super().__init__(app)
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds
        self.max_concurrent = max_concurrent
        self.exempt_paths = exempt_paths
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._inflight: dict[str, int] = defaultdict(int)

    def _key(self, request: Request) -> str:
        context = current_context()
        if context is not None and context.principal.subject != "anonymous":
            return context.principal.subject
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        key = self._key(request)
        now = time.monotonic()

        with self._lock:
            window = self._hits[key]
            while window and now - window[0] > self.window_seconds:
                window.popleft()
            if len(window) >= self.rate_limit:
                retry_after = max(0.0, self.window_seconds - (now - window[0]))
                raise RateLimited(
                    f"{key} exceeded {self.rate_limit} requests per {self.window_seconds:.0f}s",
                    retry_after_seconds=round(retry_after, 2),
                )
            window.append(now)

            if self._inflight[key] >= self.max_concurrent:
                raise ConcurrencyLimited(
                    f"{key} already has {self._inflight[key]} requests in flight; "
                    f"the limit is {self.max_concurrent}"
                )
            self._inflight[key] += 1

        try:
            return await call_next(request)
        finally:
            with self._lock:
                self._inflight[key] -= 1
                if self._inflight[key] <= 0:
                    del self._inflight[key]
