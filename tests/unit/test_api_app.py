"""App factory, routing, and typed-error mapping (contract Sections 11.3, 6.2).

These tests fail when the code is wrong, not when it is merely different: each
one names a contract clause and breaks if that clause stops holding.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from api.app import create_app, mount_routers
from api.context import Scope
from api.deps import Container
from api.middleware.auth import TokenRegistry
from api.state import LeaseRecord, TaskRecord, TimingRecord, WorkUnitRecord
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import DriftFinding, ProjectState, TaskState

OWNER = "owner-token-for-tests"
WORKER = "worker-token-for-tests"
AUTH = {"authorization": f"Bearer {OWNER}"}

#: Contract Section 11.3's representative endpoint list, verbatim.
SECTION_11_3_ENDPOINTS = [
    ("POST", "/projects/import"),
    ("POST", "/projects/{project_id}/run"),
    ("GET", "/projects/{project_id}/status"),
    ("GET", "/projects/{project_id}/graph"),
    ("GET", "/projects/{project_id}/scope-drift"),
    ("GET", "/tasks/{task_id}"),
    ("POST", "/tasks/{task_id}/resume"),
    ("GET", "/evaluations/{evaluation_id}"),
    ("GET", "/dependencies/{dependency_id}/impact"),
    ("POST", "/contracts/{contract_id}/approve"),
    ("POST", "/contracts/{contract_id}/review"),
]


@pytest.fixture
def registry() -> TokenRegistry:
    return TokenRegistry(owner_token=OWNER, service_token=None, worker_token=WORKER)


@pytest.fixture
def container() -> Container:
    return Container.build()


@pytest.fixture
def client(container: Container, registry: TokenRegistry) -> TestClient:
    app = create_app(container=container, token_registry=registry, enable_tracing=False)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def imported(client: TestClient, container: Container) -> str:
    response = client.post("/projects/import", json={"pack_root": "project-pack"}, headers=AUTH)
    assert response.status_code == 200, response.text
    project_id = response.json()["payload"]["project_id"]
    container.control_plane.upsert_task(
        TaskRecord(
            task_id="T-A",
            project_id=project_id,
            title="first",
            state=TaskState.RUNNING,
            workstream="api",
            requirement_ids=("GATE-D1-02-A1",),
            lease=LeaseRecord(
                lease_id="L-A", task_id="T-A", holder_alias="implementer-i12", fence_token=3
            ),
            timing=TimingRecord(
                queued_at="2026-08-02T05:00:00+00:00",
                claimed_at="2026-08-02T05:01:00+00:00",
                started_at="2026-08-02T05:02:00+00:00",
                completed_at="2026-08-02T05:12:00+00:00",
            ),
            work_units=(
                WorkUnitRecord(work_unit_id="WU-A", task_id="T-A", state=TaskState.RUNNING),
            ),
        )
    )
    container.control_plane.upsert_task(
        TaskRecord(
            task_id="T-B",
            project_id=project_id,
            title="second",
            state=TaskState.BLOCKED_DEPENDENCY,
            depends_on=("T-A",),
            typed_blocker="waiting on T-A",
        )
    )
    return project_id


# --------------------------------------------------------------------- routing


def test_every_section_11_3_endpoint_is_routed(client: TestClient) -> None:
    # Read the OpenAPI document rather than walking ``app.routes``: this
    # Starlette version nests included routers behind an ``_IncludedRouter``
    # wrapper, and a test that walked the wrapper would silently pass on an
    # empty set.
    schema = client.get("/openapi.json").json()
    routed = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    missing = [entry for entry in SECTION_11_3_ENDPOINTS if tuple(entry) not in routed]
    assert not missing, f"contract Section 11.3 endpoints not routed: {missing}"


def test_health_reports_wiring_facts_not_a_constant(client: TestClient, imported: str) -> None:
    body = client.get("/health").json()
    assert body["contract_id"] == CONTRACT_ID
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["projects_loaded"] == 1
    # The default runtime records rather than executes; health must say so
    # rather than claiming a graph runtime the build does not yet have.
    assert body["runtime_executes_graph"] is False


# ------------------------------------------------------- extra router mounting


def test_extra_router_mounts_inside_the_same_middleware_stack(
    container: Container, registry: TokenRegistry
) -> None:
    """AMENDMENT-001 Section 11.7: the owner surface gets no extra authority."""
    extra = APIRouter()

    @extra.get("/owner/state")
    def owner_state() -> dict:
        return {"ok": True}

    app = create_app(
        container=container, token_registry=registry, extra_routers=[extra], enable_tracing=False
    )
    client = TestClient(app, raise_server_exceptions=False)

    # Unauthenticated: the mounted router inherits authentication.
    assert client.get("/owner/state").status_code == 401
    # Authenticated: it works.
    assert client.get("/owner/state", headers=AUTH).json() == {"ok": True}
    # And it inherits version binding without opting in.
    stale = client.get(
        "/owner/state", headers={**AUTH, "x-efah-contract-version": "0.9"}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == DriftFinding.STALE_CONTRACT_VERSION


def test_extra_router_accepts_include_router_kwargs(
    container: Container, registry: TokenRegistry
) -> None:
    extra = APIRouter()

    @extra.get("/state")
    def state() -> dict:
        return {"ok": True}

    app = create_app(
        container=container,
        token_registry=registry,
        extra_routers=[(extra, {"prefix": "/owner", "tags": ["owner"]})],
        enable_tracing=False,
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/owner/state", headers=AUTH).status_code == 200


def test_mount_routers_on_an_existing_app(container: Container, registry: TokenRegistry) -> None:
    app = create_app(container=container, token_registry=registry, enable_tracing=False)
    extra = APIRouter()

    @extra.get("/later")
    def later() -> dict:
        return {"ok": True}

    mount_routers(app, [extra])
    assert TestClient(app).get("/later", headers=AUTH).status_code == 200


# ------------------------------------------------------------- happy-path uses


def test_import_run_status_graph_and_drift(client: TestClient, imported: str) -> None:
    run = client.post(f"/projects/{imported}/run", json={}, headers=AUTH)
    assert run.status_code == 200
    assert run.json()["payload"]["state"] == ProjectState.RUNNING

    status = client.get(f"/projects/{imported}/status", headers=AUTH).json()
    assert status["project_and_milestone_status"]["task_state_counts"]["RUNNING"] == 1

    graph = client.get(f"/projects/{imported}/graph", headers=AUTH).json()
    assert {node["id"] for node in graph["nodes"]} == {"T-A", "T-B"}
    assert graph["critical_path"] == ["T-A", "T-B"]
    assert graph["has_cycle"] is False

    drift = client.get(f"/projects/{imported}/scope-drift", headers=AUTH).json()
    assert drift["open_count"] == 0


def test_task_response_carries_derived_durations_and_no_estimate(
    client: TestClient, imported: str
) -> None:
    """Section 9.8: time is measured from system events, never estimated."""
    body = client.get("/tasks/T-A", headers=AUTH).json()
    assert body["derived_durations"]["queue_duration"] == 60.0
    assert body["derived_durations"]["total_wall_clock"] == 720.0
    assert "WU-A" in body["work_unit_durations"]
    assert not any("estimate" in key for key in body)
    assert not any("estimate" in key for key in body["timing"])


def test_resume_records_the_owner_answer_before_dispatch(
    client: TestClient, container: Container, imported: str
) -> None:
    response = client.post(
        "/tasks/T-B/resume", json={"owner_answer": "proceed with option B"}, headers=AUTH
    )
    assert response.status_code == 200
    decisions = container.control_plane.snapshot(imported).decisions
    assert any(d.rationale == "proceed with option B" for d in decisions.__iter__())
    assert all(d.contract_version == CONTRACT_VERSION for d in decisions)


# ---------------------------------------------------------- negative controls


def test_resume_cannot_walk_a_task_into_a_gate_only_state(
    client: TestClient, container: Container, imported: str
) -> None:
    """Section 9.3: only gates may produce PASSED."""
    container.control_plane.upsert_task(
        TaskRecord(task_id="T-P", project_id=imported, title="passed", state=TaskState.PASSED)
    )
    response = client.post("/tasks/T-P/resume", json={}, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == TaskState.FAILED_SCOPE


def test_invalid_pack_is_a_typed_contract_failure_not_a_500(client: TestClient) -> None:
    response = client.post("/projects/import", json={"pack_root": "/nope"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == ProjectState.FAILED_CONTRACT


def test_scope_expanding_instruction_is_rejected_not_executed(
    client: TestClient, container: Container, imported: str
) -> None:
    """Section 19.2 UNAPPROVED_SCOPE_EXPANSION; GATE-D1-10 A6."""
    before = container.control_plane.get_project(imported).current_run_id
    response = client.post(
        f"/projects/{imported}/run", json={"reason": "also build a billing module"}, headers=AUTH
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == DriftFinding.UNAPPROVED_SCOPE_EXPANSION
    # "rejected, not executed": no run was started.
    assert container.control_plane.get_project(imported).current_run_id == before


def test_worker_alias_cannot_be_a_real_model_identity(client: TestClient) -> None:
    """Section 11.2: task participants are known by alias only."""
    response = client.get(
        "/projects/x/status",
        headers={"authorization": f"Bearer {WORKER}", "x-efah-alias": "claude-opus-4-1-20250805"},
    )
    assert response.status_code == 401
    assert "PROTECTED_ASSET_ACCESS" in response.json()["error"]["detail"]


def test_worker_scope_cannot_approve_a_contract(client: TestClient) -> None:
    response = client.post(
        "/contracts/EFAH-CONTRACT-001/approve",
        json={"approved_version": CONTRACT_VERSION, "approver": "worker"},
        headers={"authorization": f"Bearer {WORKER}", "x-efah-alias": "implementer-i12"},
    )
    assert response.status_code == 403
    assert Scope.CONTRACT_APPROVE in response.json()["error"]["detail"]


def test_api_refuses_rather_than_opening_when_no_credential_is_configured(
    container: Container,
) -> None:
    """Section 8.1: no silent defaults, and never a default-open one."""
    app = create_app(
        container=container, token_registry=TokenRegistry(), enable_tracing=False
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/projects/x/status", headers={"authorization": "Bearer anything"})
    assert response.status_code == 401
    assert "no API credential is configured" in response.json()["error"]["detail"]


def test_every_error_body_carries_the_contract_binding(client: TestClient) -> None:
    """Section 18: a failure must be joinable to the revision that produced it."""
    for response in (
        client.get("/projects/x/status"),
        client.get("/projects/x/status", headers={"authorization": "Bearer wrong"}),
        client.post("/projects/import", json={"pack_root": "/nope"}, headers=AUTH),
        client.get("/tasks/missing", headers=AUTH),
    ):
        error = response.json()["error"]
        assert error["contract_id"] == CONTRACT_ID
        assert error["contract_version"] == CONTRACT_VERSION
        assert error["correlation_id"]
        assert error["code"]
