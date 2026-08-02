"""Application factory and composition root (contract Sections 5.1, 5.2, 11.3).

``create_app`` is the one place where ports, adapters, middleware, and routers
are assembled. Every dependency is injected; nothing is imported at request
time; nothing is a module-level singleton except the tracer provider, which
OpenTelemetry requires to be global.

Mounting an extra router (for example the owner control surface, §11.7)::

    from api.app import create_app
    from owner_surface.router import router as owner_router

    app = create_app(extra_routers=[owner_router])

or with a prefix and tags::

    app = create_app(extra_routers=[(owner_router, {"prefix": "/owner", "tags": ["owner"]})])

The extra router is included *after* the contract's own routes, inside the same
middleware stack, so it inherits authentication, version binding, drift
rejection, rate limiting, provenance, and audit without opting in. That is the
point: AMENDMENT-001 says the owner control surface "holds no authority the API
and contract do not already grant", and the cheapest way to guarantee that is
for it to be unable to escape the stack.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.deps import Container
from api.errors import ApiError, SchemaValidationFailed, api_error_handler
from api.middleware import install_middleware
from api.middleware.audit import AuditMiddleware
from api.middleware.auth import PUBLIC_PATHS, AuthenticationMiddleware, TokenRegistry
from api.middleware.version_binding import VersionBindingMiddleware
from api.router import router as contract_router
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from integrations.otel import OtelSettings, install_tracer_provider

#: What ``extra_routers`` accepts: a router, or a router plus ``include_router``
#: keyword arguments.
ExtraRouter = APIRouter | tuple[APIRouter, dict[str, Any]]

API_TITLE = "EFAH control plane"
API_DESCRIPTION = (
    "Evidence-first cross-vendor engineering harness. Endpoints map to controllers only "
    "(contract Section 11.3); workflow and model-routing decisions live in the runtime and "
    "the model router respectively."
)


def create_app(
    *,
    container: Container | None = None,
    extra_routers: Sequence[ExtraRouter] | None = None,
    token_registry: TokenRegistry | None = None,
    otel_settings: OtelSettings | None = None,
    enable_tracing: bool = True,
    middleware_options: dict[type, dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    :param container: pre-built composition container. Omit to get the default
        in-process adapters (see :class:`api.deps.Container`).
    :param extra_routers: routers to mount inside the same middleware stack.
    :param token_registry: API credentials. Defaults to the environment.
    :param otel_settings: exporter configuration. Defaults to the EFAH-dedicated
        collector on ``localhost:4319``; the forbidden ports are refused.
    :param enable_tracing: set ``False`` to build the app without installing a
        global tracer provider (tests that assert on spans install their own).
    :param middleware_options: per-middleware-class keyword overrides.
    """
    resolved_container = container or Container.build()
    registry = token_registry or TokenRegistry.from_environment()

    if enable_tracing:
        install_tracer_provider(otel_settings or OtelSettings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = resolved_container
        app.state.token_registry = registry
        yield

    app = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=f"{CONTRACT_ID}@{CONTRACT_VERSION}",
        lifespan=lifespan,
    )
    # Set outside the lifespan too, so a TestClient used without a context
    # manager still finds the container.
    app.state.container = resolved_container
    app.state.token_registry = registry

    options: dict[type, dict[str, Any]] = {
        AuthenticationMiddleware: {"registry": registry},
        AuditMiddleware: {"sink": resolved_container.audit_sink},
        VersionBindingMiddleware: {"public_paths": PUBLIC_PATHS},
    }
    for middleware_class, overrides in (middleware_options or {}).items():
        options.setdefault(middleware_class, {}).update(overrides)
    install_middleware(app, options=options)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)

    app.include_router(contract_router)
    for entry in extra_routers or ():
        _include(app, entry)

    return app


def _include(app: FastAPI, entry: ExtraRouter) -> None:
    if isinstance(entry, tuple):
        router, kwargs = entry
        app.include_router(router, **kwargs)
    else:
        app.include_router(entry)


async def _validation_handler(request, exc: RequestValidationError) -> JSONResponse:
    """Render FastAPI's validation failure as the typed schema error.

    Without this, a bad field would return FastAPI's default 422 shape, which
    carries no contract binding and no correlation id -- so the dashboard could
    not classify it and the owner could not trace it.
    """
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
        for error in exc.errors()
    )
    error = SchemaValidationFailed(detail or "request failed schema validation")
    return JSONResponse(status_code=error.status_code, content=error.body())


def mount_routers(app: FastAPI, routers: Iterable[ExtraRouter]) -> FastAPI:
    """Mount additional routers on an already-created app.

    Prefer passing ``extra_routers`` to :func:`create_app`; this exists for the
    case where the orchestrator receives an app it did not construct.
    """
    for entry in routers:
        _include(app, entry)
    return app


_DEFAULT_APP: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build the module-level ``app`` on first access, not on import.

    ``uvicorn api.app:app`` needs a module attribute, but constructing it at
    import time would install a global tracer provider -- and therefore start
    exporting to the live collector -- as a side effect of anyone importing this
    module, including a unit test that only wanted ``create_app``.
    """
    if name == "app":
        global _DEFAULT_APP
        if _DEFAULT_APP is None:
            _DEFAULT_APP = create_app()
        return _DEFAULT_APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ExtraRouter", "app", "create_app", "mount_routers"]
