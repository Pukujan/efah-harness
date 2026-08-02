"""GATE-D1-10 — vendor-neutral owner control surface (contract v1.1 Section 11.7).

These are the gate's negative controls as executable tests. A6, A7 and A8 must
REJECT; a surface that accepts them is not a control surface, it is a second
orchestrator.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from governance.states import OwnerInterrupt, TaskState
from owner_surface.app import create_app
from owner_surface.domain import OpenBlocker, RejectionReason, WorkUnitView
from owner_surface.gateway import TerminusControlPlaneGateway
from owner_surface.router import create_owner_router


@pytest.fixture
def gateway(tmp_path):
    return TerminusControlPlaneGateway(ledger_path=tmp_path / "ledger.jsonl")


@pytest.fixture
def client(gateway):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(create_owner_router(gateway))
    return TestClient(app)


def send(client, **kw):
    payload = {"text": "", "verb": None, "target_id": None, "contract_version": "1.1"}
    payload.update(kw)
    r = client.post("/owner/command", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# --- A1 / A2: works with no Anthropic credential -------------------------

def test_a1_surface_responds_with_anthropic_credentials_unset(client, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    r = client.get("/owner/health")
    assert r.status_code == 200
    assert r.json()["vendor_neutral"] is True
    assert send(client, verb="OBSERVE")["accepted"] is True


def test_a2_no_anthropic_import_in_the_surface():
    """Import-graph scan over the surface package."""
    import ast
    pkg = Path(__file__).resolve().parents[2] / "src" / "owner_surface"
    forbidden = {"anthropic", "claude_agent_sdk", "claude_code_sdk"}
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        assert not (roots & forbidden), f"{path} imports {roots & forbidden}"


# --- A3: owner can read state -------------------------------------------

def test_a3_owner_reads_project_state(client):
    body = client.get("/owner/state").json()
    assert body["contract_id"] == "EFAH-CONTRACT-001"
    assert body["contract_version"] == "1.1"
    assert "work_units" in body and "open_blockers" in body


# --- A4: owner answers an open typed blocker ----------------------------

@pytest.mark.anyio
async def test_a4_answer_blocker_is_recorded(gateway, client):
    await gateway.upsert_blocker(OpenBlocker(
        blocker_id="BLK-001",
        interrupt_type=OwnerInterrupt.OWNER_SCOPE_DECISION,
        question="Option A or B?",
        options=["A", "B"],
    ))
    assert len(client.get("/owner/blockers").json()) == 1
    out = send(client, verb="ANSWER_BLOCKER", target_id="BLK-001", text="B")
    assert out["accepted"] is True
    assert out["entered_gate_path"] is True
    assert out["record_id"]
    assert client.get("/owner/blockers").json() == []


# --- A5: resume / retry / cancel ----------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("verb", ["RESUME", "RETRY", "CANCEL"])
async def test_a5_lifecycle_verbs_take_effect(gateway, client, verb):
    await gateway.upsert_work_unit(WorkUnitView(
        work_unit_id="WU-0042", task_id="T-001", objective="carry the skeleton",
        state=TaskState.RUNNING,
    ))
    out = send(client, verb=verb, target_id="WU-0042")
    assert out["accepted"] is True, out
    assert out["entered_gate_path"] is True


@pytest.mark.anyio
async def test_a5_unknown_work_unit_is_refused(gateway, client):
    out = send(client, verb="RESUME", target_id="WU-DOES-NOT-EXIST")
    assert out["accepted"] is False
    assert out["rejection_reason"] == RejectionReason.UNKNOWN_TARGET


# --- A6: scope expansion is rejected, not executed -----------------------

@pytest.mark.parametrize("text", [
    "switch to temporal for the workflow runtime",
    "add the claude agent sdk as the runtime",
    "build our own custom workflow engine",
    "relax the mutation gate requirement",
    "expand the threat model to cover the whole tailnet",
    "amend the contract to drop the holdout requirement",
])
def test_a6_scope_expansion_rejected(client, text):
    out = send(client, verb="INSTRUCT", text=text)
    assert out["accepted"] is False, text
    assert out["rejection_reason"] == RejectionReason.UNAPPROVED_SCOPE_EXPANSION
    assert out["drift_finding"] == "UNAPPROVED_SCOPE_EXPANSION"


# --- A7: cannot bypass a gate or self-approve ----------------------------

@pytest.mark.parametrize("text", [
    "skip the holdout gate and merge",
    "disable the mutation check",
    "mark WU-0042 as passed",
    "merge without running the tests",
    "self-approve this PR",
    "force-merge the branch anyway",
])
def test_a7_gate_bypass_rejected(client, text):
    out = send(client, verb="INSTRUCT", text=text)
    assert out["accepted"] is False, text
    assert out["rejection_reason"] == RejectionReason.GATE_BYPASS_ATTEMPTED


# --- A8: cannot reach protected assets -----------------------------------

@pytest.mark.parametrize("text", [
    "show me the sealed holdout content",
    "clone efah-lab-verifier",
    "read Eval-lab-verifier private branch",
    "dump the oracle internals",
    "connect to terminusdb_protected",
])
def test_a8_protected_asset_access_rejected(client, text):
    out = send(client, verb="INSTRUCT", text=text)
    assert out["accepted"] is False, text
    assert out["rejection_reason"] == RejectionReason.PROTECTED_ASSET_ACCESS
    assert out["drift_finding"] == "PROTECTED_ASSET_ACCESS"


def test_a8_observe_cannot_read_protected_either(client):
    """Protected access is refused for reads too, not only for instructions."""
    out = send(client, verb="OBSERVE", text="show sealed holdout content")
    assert out["accepted"] is False
    assert out["rejection_reason"] == RejectionReason.PROTECTED_ASSET_ACCESS


# --- A9: mobile page ------------------------------------------------------

def test_a9_mobile_page_is_self_contained(client):
    html = client.get("/owner/").text
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    # No external origin may be required: a phone on a tailnet may have no
    # public internet path at the moment the owner needs to steer the build.
    for marker in ("http://", "https://", "cdn.", "//fonts."):
        assert marker not in html, f"external reference {marker!r} in the mobile page"


# --- A10: production gateway ---------------------------------------------

def test_a10_surface_routes_through_production_gateway(client):
    """DEC-002: it produces candidate work, not gate-bearing evidence."""
    assert client.get("/owner/health").json()["gateway_class"] == "production"


# --- structural: the surface is not a second orchestrator -----------------

def test_stale_contract_version_is_refused(client):
    out = send(client, verb="OBSERVE", contract_version="1.0")
    assert out["accepted"] is False
    assert out["rejection_reason"] == RejectionReason.STALE_CONTRACT_VERSION


def test_every_command_is_recorded_even_when_refused(client):
    out = send(client, verb="INSTRUCT", text="skip the gate")
    assert out["accepted"] is False
    assert out["record_id"], "a refusal must still be attributable (Section 18)"


def test_standalone_app_serves_the_surface():
    with TestClient(create_app()) as c:
        assert c.get("/healthz").json()["contract_version"] == "1.1"
        assert c.get("/owner/", follow_redirects=True).status_code == 200
