"""Plane projection adapter (contract Sections 4.1, 9.8, 11.6; GATE-D2-11).

The offline tests run everywhere and use a scripted transport that replays the
**measured** responses of the live API -- including its inconsistencies (409 for
a duplicate work item, 400 for a duplicate cycle). A fake that only replays the
happy path would let a regression through.

The live tests hit the real workspace and are skipped without ``$PLANE_API_KEY``
or ``--run-live``. They create objects stamped ``external_source=efah-projection``
and delete them again, so the owner's project is left as it was found.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import yaml

from api.adapters.control_plane_memory import InMemoryControlPlane
from api.state import (
    ControlPlaneSnapshot,
    DecisionRecord,
    EvaluationRecord,
    LeaseRecord,
    ProjectRecord,
    ReleaseRecord,
    TaskRecord,
    TimingRecord,
    WorkUnitRecord,
)
from dashboard.redaction import ProtectedContentLeak
from dashboard.source import ReadOnlySource
from governance.states import ProjectState, TaskState, Verdict
from integrations.pack import load_pack
from integrations.secrets import SecretResolver
from integrations.plane import (
    DERIVED_DURATION_KEYS,
    EXTERNAL_SOURCE,
    PROJECTION_MAPPING,
    AuthoritativeMutationAttempted,
    PlaneClient,
    PlaneConfig,
    PlaneProjection,
    PlaneUnavailable,
    derived_worklog,
)
from observability.identity import ProtectedIdentityLeak

LIVE = bool(os.environ.get("PLANE_API_KEY"))
live_only = pytest.mark.skipif(not LIVE, reason="PLANE_API_KEY is not set")


@pytest.fixture
def config() -> PlaneConfig:
    return PlaneConfig.from_pack(load_pack("project-pack"))


@pytest.fixture
def snapshot() -> ControlPlaneSnapshot:
    control_plane = InMemoryControlPlane()
    control_plane.import_project(
        pack_root="project-pack", requested_by="test", correlation_id="c"
    )
    control_plane.upsert_task(
        TaskRecord(
            task_id="T-1",
            project_id="EFAH-001",
            title="a task",
            state=TaskState.RUNNING,
            workstream="api",
            lease=LeaseRecord(
                lease_id="L-1", task_id="T-1", holder_alias="implementer-i12", worktree="wt/a"
            ),
            timing=TimingRecord(
                queued_at="2026-08-02T05:00:00+00:00",
                claimed_at="2026-08-02T05:00:30+00:00",
                started_at="2026-08-02T05:01:00+00:00",
                completed_at="2026-08-02T05:11:00+00:00",
            ),
            work_units=(
                WorkUnitRecord(work_unit_id="WU-1", task_id="T-1", state=TaskState.RUNNING),
            ),
        )
    )
    control_plane.upsert_task(
        TaskRecord(
            task_id="T-2",
            project_id="EFAH-001",
            title="blocked task",
            state=TaskState.BLOCKED_DEPENDENCY,
            typed_blocker="waiting",
        )
    )
    control_plane.upsert_evaluation(
        "EFAH-001",
        EvaluationRecord(
            evaluation_id="EV-1",
            task_id="T-1",
            visible_verdict=Verdict.PASS,
            hidden_suite_verdict=Verdict.PASS,
            hidden_assertions_total=2,
        ),
    )
    return control_plane.snapshot("EFAH-001")


# ------------------------------------------------- scripted live behaviour


class ScriptedPlane:
    """Replays the API's measured behaviour, including its inconsistencies."""

    def __init__(self, *, unreachable: bool = False, forbidden: bool = False) -> None:
        self.unreachable = unreachable
        self.forbidden = forbidden
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        if self.forbidden:
            return httpx.Response(403, json={"detail": "Given API token is not valid"})

        parts = [segment for segment in request.url.path.split("/") if segment]
        collection = parts[-1] if not parts[-1].endswith("/") else parts[-2]

        if request.method == "GET":
            if collection == "total-worklogs":
                return httpx.Response(404, json={"message": "Worklog is not enabled for the project"})
            external_id = request.url.params.get("external_id")
            if external_id:
                found = self.objects.get((parts[-1], external_id))
                if found is None:
                    return httpx.Response(404, json={"error": "The requested resource does not exist."})
                # Measured: work-items and modules resolve to a single object.
                if parts[-1] in ("work-items", "modules"):
                    return httpx.Response(200, json=found)
            return httpx.Response(
                200,
                json={
                    "results": [
                        value for (coll, _), value in self.objects.items() if coll == parts[-1]
                    ]
                },
            )

        if request.method == "POST":
            body = json.loads(request.content)
            key = (collection, body.get("external_id"))
            if key in self.objects:
                if collection == "cycles":
                    # Measured: cycles enforce name uniqueness and give no id.
                    return httpx.Response(
                        400, json={"name": ["A cycle with this name already exists in the project."]}
                    )
                return httpx.Response(
                    409,
                    json={
                        "error": "Issue with the same external id and external source already exists",
                        "id": self.objects[key]["id"],
                    },
                )
            record = dict(body)
            record["id"] = f"{collection}-{len(self.objects)}"
            self.objects[key] = record
            return httpx.Response(201, json=record)

        if request.method == "PATCH":
            object_id = parts[-1]
            for record in self.objects.values():
                if record["id"] == object_id:
                    record.update(json.loads(request.content))
                    return httpx.Response(200, json=record)
            return httpx.Response(404, json={"error": "not found"})

        if request.method == "DELETE":
            for key, record in list(self.objects.items()):
                if record["id"] == parts[-1]:
                    del self.objects[key]
                    return httpx.Response(204)
            return httpx.Response(404)

        return httpx.Response(405)


def scripted(config: PlaneConfig, **kwargs) -> tuple[PlaneProjection, ScriptedPlane]:
    plane = ScriptedPlane(**kwargs)
    client = PlaneClient(
        config,
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(plane.handler)),
    )
    return PlaneProjection(config, client=client), plane


# --------------------------------------------------------- configuration


def test_config_comes_from_the_pack(config: PlaneConfig) -> None:
    assert config.workspace == "efah"
    assert config.project_id == "0f843a48-6969-498c-9d60-64f41147bbb2"
    assert config.mode == "projection_only"
    assert config.poll_interval_seconds == 30
    assert config.outage_blocks_project is False
    assert config.outage_state == "degraded_projection"
    assert config.contains_plane_sample_data is True


def test_writes_are_routed_to_the_api_host(config: PlaneConfig) -> None:
    """Measured: app.plane.so answers 405 to every write; api.plane.so accepts."""
    assert config.base_url == "https://app.plane.so"
    assert config.api_host == "https://api.plane.so"
    assert config.project_root.startswith("https://api.plane.so/api/v1/workspaces/efah/projects/")


def test_projection_mapping_matches_plane_yaml() -> None:
    pack_mapping = yaml.safe_load(open("project-pack/plane.yaml"))["projection_mapping"]
    assert PROJECTION_MAPPING == pack_mapping


# ------------------------------------------------- one-way, never truth


def test_the_projection_declares_itself_non_authoritative(config: PlaneConfig) -> None:
    projection = PlaneProjection(config)
    assert projection.may_mutate_authoritative_state is False
    assert projection.writes_flow == "terminusdb_to_plane_one_way"


def test_write_back_fails_loudly(config: PlaneConfig) -> None:
    with pytest.raises(AuthoritativeMutationAttempted):
        PlaneProjection(config).write_back()


def test_a_writable_control_plane_is_refused_as_a_source(config: PlaneConfig) -> None:
    with pytest.raises(AuthoritativeMutationAttempted):
        PlaneProjection(config, source=InMemoryControlPlane())  # type: ignore[arg-type]
    # ...and the read-only handle is accepted.
    PlaneProjection(config, source=ReadOnlySource(InMemoryControlPlane()))


# ------------------------------------------------------------ projecting


def test_payload_maps_every_entity_per_the_mapping(config, snapshot) -> None:
    projection, _ = scripted(config)
    payload = projection.build_payload(snapshot)
    external_ids = {item["external_id"] for item in payload["work_items"]}
    assert "task:T-1" in external_ids  # Task -> work_item
    assert "workunit:WU-1" in external_ids  # WorkUnit -> sub_work_item
    assert "release:RC-EFAH-001" in external_ids  # ReleaseCandidate -> work_item
    assert {m["external_id"] for m in payload["modules"]} == {"workstream:api"}  # Workstream
    assert payload["cycles"]  # Milestone -> cycle
    assert payload["evaluation_properties"][0]["properties"]["visible_verdict"] == "PASS"


def test_a_blocked_task_projects_as_blocked(config, snapshot) -> None:
    """Blocker -> work_item_with_blocked_state."""
    projection, _ = scripted(config)
    payload = projection.build_payload(snapshot)
    blocked = next(i for i in payload["work_items"] if i["external_id"] == "task:T-2")
    assert blocked["body"]["priority"] == "urgent"


def test_projection_creates_then_updates_rather_than_duplicating(config, snapshot) -> None:
    projection, plane = scripted(config)
    first = projection.project(snapshot)
    assert first.status == "projected"
    assert first.work_items_upserted == 4
    assert first.errors == ()

    created = len(plane.objects)
    second = projection.project(snapshot)
    assert second.status == "projected"
    assert len(plane.objects) == created, "second pass created duplicates"
    assert any(method == "PATCH" for method, _ in plane.calls)


def test_a_sub_work_item_binds_to_its_parent(config, snapshot) -> None:
    """Measured: there is no sub-work-items endpoint; `parent` is the mechanism."""
    projection, plane = scripted(config)
    projection.project(snapshot)
    parent = plane.objects[("work-items", "task:T-1")]
    child = plane.objects[("work-items", "workunit:WU-1")]
    assert child["parent"] == parent["id"]


def test_everything_created_is_tagged_so_sample_data_stays_separable(config, snapshot) -> None:
    """plane.yaml: the project ships with Plane sample data."""
    projection, plane = scripted(config)
    projection.project(snapshot)
    assert all(record["external_source"] == EXTERNAL_SOURCE for record in plane.objects.values())


def test_cycle_name_collision_is_resolved_by_external_id(config, snapshot) -> None:
    """Cycles answer 400 with no id, unlike work items' 409."""
    projection, plane = scripted(config)
    projection.project(snapshot)
    cycles_before = sum(1 for coll, _ in plane.objects if coll == "cycles")
    result = projection.project(snapshot)
    assert result.errors == ()
    assert sum(1 for coll, _ in plane.objects if coll == "cycles") == cycles_before


# --------------------------------------------------------------- outage


def test_an_unreachable_plane_degrades_and_does_not_block_the_project(
    config, snapshot
) -> None:
    """plane.yaml: outage_blocks_project false, outage_state degraded_projection."""
    projection, _ = scripted(config, unreachable=True)
    result = projection.project(snapshot)
    assert result.status == "degraded_projection"
    assert result.blocks_project is False
    assert projection.is_available() is False


def test_a_rejected_credential_degrades_rather_than_raising(config, snapshot) -> None:
    projection, _ = scripted(config, forbidden=True)
    result = projection.project(snapshot)
    assert result.status == "degraded_projection"
    assert result.blocks_project is False


def test_a_missing_credential_degrades_rather_than_raising(config, snapshot) -> None:
    # An empty environment, not api_key=None: None means "resolve from the
    # environment", and the environment here has a real key.
    client = PlaneClient(config, client=httpx.Client(), resolver=SecretResolver(environ={}))
    assert client.has_credential is False
    result = PlaneProjection(config, client=client).project(snapshot)
    assert result.status == "degraded_projection"


def test_transport_errors_never_escape_the_adapter(config) -> None:
    """Section 5.1: the vendor stays inside the adapter."""
    client = PlaneClient(
        config,
        api_key="k",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ReadTimeout("timeout", request=request)
                )
            )
        ),
    )
    with pytest.raises(PlaneUnavailable):
        client.request("GET", "/states/")


# ----------------------------------------------------- Section 9.8 worklog


def test_worklog_exposes_every_derived_duration_key_and_no_estimate() -> None:
    worklog = derived_worklog(
        TimingRecord(
            queued_at="2026-08-02T05:00:00+00:00",
            claimed_at="2026-08-02T05:00:30+00:00",
            started_at="2026-08-02T05:01:00+00:00",
            completed_at="2026-08-02T05:11:00+00:00",
        )
    )
    assert set(worklog) == set(DERIVED_DURATION_KEYS)
    assert worklog["queue_duration"] == 30.0
    # Not derivable from control-plane events; None, never a guess.
    assert worklog["model_call_duration"] is None


def test_derived_duration_keys_match_plane_yaml() -> None:
    pack = yaml.safe_load(open("project-pack/plane.yaml"))["worklog"]
    assert pack["source"] == "system_events_only"
    assert pack["agent_estimates_permitted"] is False
    assert list(DERIVED_DURATION_KEYS) == list(pack["derived_durations"])


def test_durations_appear_on_the_projected_work_item(config, snapshot) -> None:
    projection, _ = scripted(config)
    payload = projection.build_payload(snapshot)
    task = next(i for i in payload["work_items"] if i["external_id"] == "task:T-1")
    assert "Derived durations" in task["body"]["description_html"]
    assert "system events only" in task["body"]["description_html"]


# ------------------------------------------- protected content (GATE-D2-11 A3)


def test_a_leaking_snapshot_is_refused_before_the_first_network_call(config) -> None:
    """Catch it locally, not after half of it is already published."""
    projection, plane = scripted(config)
    leaking = ControlPlaneSnapshot(
        project=ProjectRecord(
            project_id="P",
            name="P",
            state=ProjectState.RUNNING,
            contract_id="EFAH-CONTRACT-001",
            contract_version="1.1",
        ),
        decisions=(
            DecisionRecord(
                decision_id="D-1",
                title="leak",
                outcome="x",
                decided_by="owner",
                decided_at="now",
                contract_version="1.1",
                rationale="the mutant patch was: assert result == 42",
            ),
        ),
        release=ReleaseRecord(release_id="RC-1"),
    )
    with pytest.raises(ProtectedContentLeak):
        projection.project(leaking)
    assert plane.calls == [], "a network call was made before the guard ran"


def test_a_real_model_identity_never_reaches_plane(config, snapshot) -> None:
    projection, _ = scripted(config)
    payload = projection.build_payload(snapshot)
    payload["work_items"][0]["body"]["name"] = "run on claude-opus-4-1-20250805"
    from dashboard.redaction import assert_no_protected_content

    with pytest.raises(ProtectedIdentityLeak):
        assert_no_protected_content(payload, where="plane_projection")


# ------------------------------------------------------------------ live


@live_only
def test_live_plane_is_reachable_and_reports_its_worklog_state(config) -> None:
    client = PlaneClient(config)
    assert client.has_credential
    assert client.health() is True
    # Measured 2026-08-02: worklogs are disabled for this project.
    assert client.worklogs_available() is False


@live_only
def test_live_round_trip_projects_and_cleans_up(config, snapshot) -> None:
    client = PlaneClient(config)
    projection = PlaneProjection(config, client=client)
    result = projection.project(snapshot)
    try:
        assert result.status == "projected"
        assert result.errors == ()
        ours = [
            item
            for item in client.list_work_items()
            if item.get("external_source") == EXTERNAL_SOURCE
        ]
        assert {item["external_id"] for item in ours} >= {"task:T-1", "workunit:WU-1"}
        # Idempotent: a second pass updates, it does not duplicate.
        assert projection.project(snapshot).status == "projected"
        again = [
            item
            for item in client.list_work_items()
            if item.get("external_source") == EXTERNAL_SOURCE
        ]
        assert len(again) == len(ours)
    finally:
        for collection, lister in (
            ("work-items", client.list_work_items),
            ("modules", client.list_modules),
            ("cycles", client.list_cycles),
        ):
            for item in lister():
                if item.get("external_source") == EXTERNAL_SOURCE:
                    client.delete(collection, item["id"])
