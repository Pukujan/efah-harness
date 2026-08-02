"""Contract and project version binding (contract Sections 11.4, 19.2).

The load-bearing one. Every request is bound to the governing contract revision
-- ``EFAH-CONTRACT-001 v1.1`` -- and a request that declares a different one is
refused with :class:`governance.states.DriftFinding.STALE_CONTRACT_VERSION`
before it reaches a controller.

Why refuse rather than upgrade: a caller that believes it is talking to v1.0 has
built its request against v1.0's semantics. Silently executing it under v1.1
produces work that traces to a revision nobody approved, which is exactly the
drift Section 19 exists to catch. The client re-reads the contract; the harness
does not guess on its behalf.

v1.1 is v1.0 plus an additive amendment, so a declaration of ``1.0`` is accepted
*for reads* -- the pack loader makes the same allowance for pack files written
before AMENDMENT-001 -- but never for a write, because a write recorded against
a superseded revision is unbound evidence.
"""

from __future__ import annotations

from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import current_context
from api.errors import StaleContractVersion
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION

CONTRACT_VERSION_HEADER: Final = "x-efah-contract-version"
CONTRACT_ID_HEADER: Final = "x-efah-contract-id"
PROJECT_ID_HEADER: Final = "x-efah-project-id"

#: Revisions v1.1 additively supersedes. Readable, not writable.
SUPERSEDED_READABLE_VERSIONS: Final = frozenset({"1.0"})

_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class VersionBindingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, public_paths: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        self.public_paths = public_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self.public_paths:
            return await call_next(request)

        declared_id = request.headers.get(CONTRACT_ID_HEADER)
        if declared_id is not None and declared_id != CONTRACT_ID:
            raise StaleContractVersion(f"{declared_id}@?", f"{CONTRACT_ID}@{CONTRACT_VERSION}")

        declared = request.headers.get(CONTRACT_VERSION_HEADER)
        if declared is not None and declared != CONTRACT_VERSION:
            readable = declared in SUPERSEDED_READABLE_VERSIONS
            if not readable or request.method not in _SAFE_METHODS:
                raise StaleContractVersion(declared, CONTRACT_VERSION)

        context = current_context()
        if context is not None:
            context.contract_id = CONTRACT_ID
            context.contract_version = CONTRACT_VERSION
            context.project_id = request.headers.get(PROJECT_ID_HEADER) or _project_id_from_path(
                request.url.path
            )
        return await call_next(request)


def _project_id_from_path(path: str) -> str | None:
    """Pull the project id out of ``/projects/{id}/...``.

    Read from the raw path rather than ``request.path_params``: this middleware
    runs before routing, so ``path_params`` is still empty here. Without this the
    project binding -- and the ``efah.project_id`` span attribute Section 23
    requires -- would be present only when the caller happened to send the
    header.
    """
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2 and segments[0] == "projects" and segments[1] != "import":
        return segments[1]
    return None
