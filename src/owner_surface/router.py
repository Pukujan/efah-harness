"""FastAPI router for the owner control surface (contract v1.1 §11.7).

Mounted onto the existing FastAPI application. It maps endpoints to the surface
graph only — §11.3's rule that a router holds no workflow or model-routing
decisions applies here too.

Reachability: bound to the operator's private network (tailnet). It is not
exposed publicly, and it holds no authority the API and contract do not already
grant.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION

from .gateway import ControlPlaneGateway, TerminusControlPlaneGateway
from .graph import GATEWAY_CLASS, build_graph
from .web import MOBILE_PAGE


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    verb: str | None = None
    target_id: str | None = None
    contract_version: str = Field(default=CONTRACT_VERSION)


class CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    verb: str
    message: str
    rejection_reason: str | None = None
    drift_finding: str | None = None
    record_id: str | None = None
    terminus_commit: str | None = None
    entered_gate_path: bool = False
    view: dict[str, Any] | None = None


def create_owner_router(gateway: ControlPlaneGateway | None = None) -> APIRouter:
    """Build the router. Pass a gateway to inject a different control plane."""
    gw = gateway or TerminusControlPlaneGateway()
    graph = build_graph(gw)
    router = APIRouter(prefix="/owner", tags=["owner-control-surface"])

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def page() -> HTMLResponse:
        """The mobile surface. 390px-first, works with no JS framework."""
        return HTMLResponse(MOBILE_PAGE)

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "surface": "owner-control-surface",
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "clause": "11.7",
            "gateway_class": GATEWAY_CLASS,
            "vendor_neutral": True,
        }

    @router.get("/state")
    async def state() -> dict[str, Any]:
        """Read projection of authoritative state (§5.1: read-only)."""
        view = await gw.project_view()
        return view.model_dump(mode="json")

    @router.get("/blockers")
    async def blockers() -> list[dict[str, Any]]:
        return [b.model_dump(mode="json") for b in await gw.open_blockers()]

    @router.post("/command", response_model=CommandResponse)
    async def command(request: CommandRequest) -> CommandResponse:
        result = await graph.ainvoke(
            {
                "raw_text": request.text,
                "explicit_verb": request.verb,
                "target_id": request.target_id,
                "contract_version": request.contract_version,
            }
        )
        outcome = result.get("outcome")
        if outcome is None:  # pragma: no cover - the graph always sets an outcome
            raise HTTPException(status_code=500, detail="surface produced no outcome")
        return CommandResponse(
            accepted=outcome.accepted,
            verb=str(outcome.verb),
            message=outcome.message,
            rejection_reason=str(outcome.rejection_reason) if outcome.rejection_reason else None,
            drift_finding=str(outcome.drift_finding) if outcome.drift_finding else None,
            record_id=outcome.record_id,
            terminus_commit=outcome.terminus_commit,
            entered_gate_path=outcome.entered_gate_path,
            view=result.get("view"),
        )

    return router
