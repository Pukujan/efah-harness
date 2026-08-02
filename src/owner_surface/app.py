"""Standalone ASGI entry point for the owner control surface.

The surface is a router mounted on the main FastAPI application (contract
Section 11.7). This module exists so it can also be served on its own -- which
is what makes it survivable: if the rest of the control plane is mid-repair, the
owner can still reach the surface and see that, rather than seeing nothing.

Bind to the tailnet address so it is reachable from a phone on the private
network and from nowhere else:

    uvicorn owner_surface.app:app --host 100.93.66.35 --port 8088
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION

from .router import create_owner_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="EFAH owner control surface",
        version=CONTRACT_VERSION,
        description=(
            f"{CONTRACT_ID} v{CONTRACT_VERSION} Section 11.7. Vendor-neutral. "
            "Holds no authority the API and contract do not already grant."
        ),
    )
    app.include_router(create_owner_router())

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/owner/")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "contract_version": CONTRACT_VERSION}

    return app


app = create_app()
