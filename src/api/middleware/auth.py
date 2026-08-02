"""Authentication, authorization, and identity (contract Section 11.4).

Three identity kinds, because the contract names three: **human** (the owner),
**service** (CI, the runtime, the projection sync), and **alias** (a worker
session, which is known by its blinded alias and never by a vendor identity).

Credentials are read through :class:`integrations.secrets.SecretResolver`, so a
token lives in the environment and never in a config file (Section 6). Tokens
are compared with :func:`hmac.compare_digest` -- a token check that leaks timing
is a token check.

Authorization is *scope-based and default-deny*. An unlisted route requires an
authenticated principal; a route's required scope is declared on the route
itself via :func:`api.middleware.auth.requires`.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import (
    ALIAS_SCOPES,
    OWNER_SCOPES,
    SERVICE_SCOPES,
    IdentityKind,
    Principal,
    Scope,
    current_context,
)
from api.errors import Unauthenticated, Unauthorized
from integrations.secrets import SecretRef, SecretResolver
from observability.identity import ProtectedIdentityLeak, assert_alias_only

AUTHORIZATION_HEADER: Final = "authorization"
ALIAS_HEADER: Final = "x-efah-alias"
IDENTITY_HEADER: Final = "x-efah-identity"

#: Paths that must answer before a credential exists: liveness and the schema
#: document. Deliberately tiny and explicit -- an open-prefix rule ("anything
#: under /public") is how an auth bypass gets introduced later.
PUBLIC_PATHS: Final = frozenset({"/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"})


@dataclass
class TokenRegistry:
    """Static credential map resolved from the environment.

    Owner and service tokens are separate values on purpose: DEC-002-style
    separation applies to control as well as to models, and a CI token that can
    answer an owner blocker is a control-plane privilege escalation.
    """

    owner_token: str | None = None
    service_token: str | None = None
    worker_token: str | None = None
    #: When no token is configured at all the API refuses every non-public
    #: request rather than opening up. Section 8.1: no silent defaults.
    require_credentials: bool = True
    extra_service_tokens: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls, resolver: SecretResolver | None = None) -> TokenRegistry:
        resolver = resolver or SecretResolver()

        def optional(name: str) -> str | None:
            return resolver.resolve(SecretRef(name=name, reference=f"env:{name}", required=False))

        return cls(
            owner_token=optional("EFAH_OWNER_TOKEN"),
            service_token=optional("EFAH_SERVICE_TOKEN"),
            worker_token=optional("EFAH_WORKER_TOKEN"),
        )

    def any_configured(self) -> bool:
        return any((self.owner_token, self.service_token, self.worker_token))

    def identify(self, presented: str, *, alias: str | None) -> Principal | None:
        """Constant-time match of a bearer token to a principal."""
        if self.owner_token and hmac.compare_digest(presented, self.owner_token):
            return Principal(
                kind=IdentityKind.HUMAN,
                subject="owner",
                scopes=frozenset(OWNER_SCOPES),
                display_name="project owner",
            )
        if self.service_token and hmac.compare_digest(presented, self.service_token):
            return Principal(
                kind=IdentityKind.SERVICE,
                subject="service",
                scopes=frozenset(SERVICE_SCOPES),
            )
        if self.worker_token and hmac.compare_digest(presented, self.worker_token):
            # A worker session identifies itself by alias. Without one it is not
            # attributable, and an unattributable write has no provenance.
            if not alias:
                raise Unauthenticated(
                    f"a worker credential must present its blinded alias in {ALIAS_HEADER}"
                )
            return Principal(
                kind=IdentityKind.ALIAS,
                subject=f"alias:{alias}",
                scopes=frozenset(ALIAS_SCOPES),
                alias=alias,
            )
        for subject, token in self.extra_service_tokens.items():
            if hmac.compare_digest(presented, token):
                return Principal(
                    kind=IdentityKind.SERVICE, subject=subject, scopes=frozenset(SERVICE_SCOPES)
                )
        return None


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Resolves the caller into a :class:`Principal` or refuses the request."""

    def __init__(self, app, registry: TokenRegistry) -> None:
        super().__init__(app)
        self.registry = registry

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        context = current_context()
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get(AUTHORIZATION_HEADER, "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("a bearer credential is required")

        alias = request.headers.get(ALIAS_HEADER)
        if alias:
            try:
                assert_alias_only(alias, field=ALIAS_HEADER)
            except ProtectedIdentityLeak as exc:
                # A caller identifying itself by a real model name would stamp a
                # vendor identity onto every record it touches (Section 11.2).
                raise Unauthenticated(str(exc)) from exc

        if not self.registry.any_configured():
            raise Unauthenticated(
                "no API credential is configured; set EFAH_OWNER_TOKEN, EFAH_SERVICE_TOKEN, "
                "or EFAH_WORKER_TOKEN. The API refuses rather than defaulting to open."
            )

        principal = self.registry.identify(token, alias=alias)
        if principal is None:
            raise Unauthenticated("the presented credential is not recognised")

        declared_kind = request.headers.get(IDENTITY_HEADER)
        if declared_kind and declared_kind != str(principal.kind):
            raise Unauthorized(
                f"credential resolves to identity kind {principal.kind!s}, "
                f"but the request declares {declared_kind!r}"
            )

        if context is not None:
            context.principal = principal
        return await call_next(request)


def requires(*scopes: Scope):
    """Route dependency asserting the caller holds every named scope.

    Returns a FastAPI dependency callable. Declaring the scope on the route
    keeps authorization next to the thing being authorized, and keeps the router
    free of policy branching (Section 11.3).
    """

    async def _check() -> Principal:
        context = current_context()
        if context is None:
            raise Unauthenticated("no request context")
        principal = context.principal
        if principal.kind is IdentityKind.ANONYMOUS:
            raise Unauthenticated("this endpoint requires an authenticated principal")
        missing = [str(scope) for scope in scopes if not principal.has(scope)]
        if missing:
            raise Unauthorized(
                f"principal {principal.subject!r} lacks required scope(s): {', '.join(missing)}"
            )
        return principal

    return _check
