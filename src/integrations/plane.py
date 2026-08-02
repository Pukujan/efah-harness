"""Plane projection adapter (contract Sections 4, 4.1, 9.8, 11.6, 23).

**Plane is a projection, never truth.** ``plane.yaml`` states it three ways --
``is_source_of_truth: false``, ``may_mutate_authoritative_state: false``,
``writes_flow: terminusdb_to_plane_one_way`` -- and this adapter is built so the
opposite is not expressible: it accepts a frozen
:class:`api.state.ControlPlaneSnapshot`, it holds a
:class:`dashboard.source.ReadOnlySource` at most, and it exposes no method that
returns anything a caller could write back into authoritative state.

**An outage is not a failure of the project.** ``outage_blocks_project: false``,
``outage_state: degraded_projection``. Every network path here converges on a
:class:`ProjectionResult` with ``status='degraded_projection'`` rather than an
exception, so a Plane outage can never be mistaken for
``FAILED_INFRASTRUCTURE``.

Endpoint shapes below were **probed against the live API on 2026-08-02**, not
inferred; the Context7 snapshot is cached at
``project-pack/evidence/context7-snapshots/``. Measured facts that documentation
alone would not have given:

* ``/work-items/`` and the legacy ``/issues/`` are both live; ``/work-items/`` is
  the documented spelling and is used here.
* There is **no** ``sub-work-items`` endpoint (404). A sub work item is a work
  item carrying ``parent`` -- which is how ``WorkUnit -> sub_work_item`` is
  implemented.
* ``POST`` with a duplicate ``external_id`` returns **409 with the existing
  ``id`` in the body**. That makes a genuine upsert possible without a
  read-before-write race: POST, and on 409 PATCH the id the server just named.
* Worklogs return ``404 {"message": "Worklog is not enabled for the project"}``.
  The feature is off for this project, so derived durations are projected onto
  the work item itself and ``worklog_api_available`` is reported false rather
  than the durations being silently dropped.
* An invalid key returns **403**, not 401.
* ``app.plane.so`` -- the host ``environments.yaml`` records -- serves **GET**
  but answers **405** to every write. ``api.plane.so`` is the write host. The
  configured base is therefore mapped to the API host rather than "corrected" in
  the pack, because the pack records the owner's fact and this is a transport
  detail the adapter owns (Section 5.1).
* ``POST /cycles/`` requires ``project_id`` **in the body** as well as in the
  path, and rejects the spelling ``project`` with
  ``{"non_field_errors": ["Project ID is required"]}``.
* Collections do **not** agree on conflict behaviour. Work items 409 with the
  existing id; cycles enforce a separate *name* uniqueness rule and answer
  ``400 {"name": ["A cycle with this name already exists in the project."]}``
  with no id at all. And the ``?external_source=&external_id=`` filter resolves
  to a single object for work items and modules, but is ignored by cycles, which
  return the whole list. :meth:`PlaneClient.upsert` therefore falls back to a
  client-side match on ``external_id`` -- assuming one uniform upsert idiom
  across collections would have produced duplicate cycles every poll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping

import httpx

from api.state import ControlPlaneSnapshot, DecisionRecord, TaskRecord, TimingRecord
from dashboard.projections import derived_durations
from dashboard.redaction import assert_no_protected_content
from dashboard.source import ReadOnlySource
from governance.states import TaskState
from integrations.secrets import SecretRef, SecretResolver
from observability.spans import Correlation, SpanKindName, efah_span

LOGGER: Final = logging.getLogger("efah.plane")

#: ``plane.yaml -> projection_mapping``. Held here as data so the contract test
#: can assert the code and the pack still agree.
PROJECTION_MAPPING: Final[dict[str, str]] = {
    "Workstream": "module",
    "Milestone": "cycle",
    "Phase": "module_or_label",
    "Task": "work_item",
    "WorkUnit": "sub_work_item",
    "Blocker": "work_item_with_blocked_state",
    "Decision": "work_item_comment_and_link",
    "EvaluationRun": "work_item_property",
    "ReleaseCandidate": "work_item",
}

#: ``plane.yaml -> worklog.derived_durations``. Section 9.8: system events only.
DERIVED_DURATION_KEYS: Final = (
    "queue_duration",
    "active_duration",
    "blocked_duration",
    "model_call_duration",
    "tool_duration",
    "evaluation_duration",
    "human_wait_duration",
    "rework_duration",
    "total_wall_clock",
)

#: Section 9.3 states that map to Plane's blocked presentation.
BLOCKED_STATES: Final = frozenset(
    {
        TaskState.BLOCKED_DEPENDENCY,
        TaskState.BLOCKED_OWNER_DECISION,
        TaskState.BLOCKED_EXTERNAL_ACCESS,
    }
)

#: ``external_source`` stamped on every object this adapter creates, so the
#: projection is separable from the Plane sample data the project shipped with
#: (``plane.yaml -> contains_plane_sample_data: true``).
EXTERNAL_SOURCE: Final = "efah-projection"

DEGRADED: Final = "degraded_projection"


class PlaneUnavailable(RuntimeError):
    """Plane could not be reached or refused the request.

    Caught inside this module and converted into a degraded
    :class:`ProjectionResult`. It is never allowed to reach the control plane,
    because ``outage_blocks_project`` is ``false``.
    """


class AuthoritativeMutationAttempted(RuntimeError):
    """Something tried to make the projection write back. Section 4.1."""


@dataclass(frozen=True)
class PlaneConfig:
    """Resolved from ``plane.yaml`` and ``environments.yaml``."""

    workspace: str
    project_id: str
    base_url: str = "https://app.plane.so"
    mode: str = "projection_only"
    poll_interval_seconds: int = 30
    outage_blocks_project: bool = False
    outage_state: str = DEGRADED
    contains_plane_sample_data: bool = False
    timeout_seconds: float = 20.0

    @classmethod
    def from_pack(cls, pack: Any, *, environment: str = "dev") -> "PlaneConfig":
        plane = pack.yaml("plane.yaml")
        block = plane["plane"]
        sync = plane.get("sync", {})
        environments = pack.yaml("environments.yaml")["environments"][environment]
        base_url = environments.get("plane", {}).get("base_url", "https://app.plane.so")
        return cls(
            workspace=str(block["workspace"]),
            project_id=str(block["project_id"]),
            base_url=str(base_url).rstrip("/"),
            mode=str(block.get("mode", "projection_only")),
            poll_interval_seconds=int(sync.get("poll_interval_seconds", 30)),
            outage_blocks_project=bool(sync.get("outage_blocks_project", False)),
            outage_state=str(sync.get("outage_state", DEGRADED)),
            contains_plane_sample_data=bool(block.get("contains_plane_sample_data", False)),
        )

    @property
    def api_host(self) -> str:
        """The host that accepts writes.

        Measured 2026-08-02: ``app.plane.so`` answers GET but returns 405 to
        POST/PATCH/DELETE; ``api.plane.so`` accepts both. ``environments.yaml``
        records the owner-facing URL, so the mapping lives here rather than as
        an edit to the pack.
        """
        if "app.plane.so" in self.base_url:
            return "https://api.plane.so"
        return self.base_url

    @property
    def api_root(self) -> str:
        return f"{self.api_host}/api/v1/workspaces/{self.workspace}"

    @property
    def project_root(self) -> str:
        return f"{self.api_root}/projects/{self.project_id}"


@dataclass
class ProjectionResult:
    """What one projection pass did, or why it could not.

    ``status`` is either ``'projected'`` or ``'degraded_projection'``. There is
    no failure status, because a Plane outage is not a project failure.
    """

    status: str
    project_id: str
    work_items_upserted: int = 0
    modules_upserted: int = 0
    cycles_upserted: int = 0
    comments_posted: int = 0
    links_posted: int = 0
    worklog_api_available: bool = False
    detail: str = ""
    errors: tuple[str, ...] = ()
    payload_preview: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_project(self) -> bool:
        """Always ``False``. ``plane.yaml -> outage_blocks_project: false``."""
        return False


class PlaneClient:
    """Thin HTTP client. The only place this project speaks Plane's wire format.

    Every transport-level failure becomes :class:`PlaneUnavailable`; no caller
    ever sees an ``httpx`` exception, which keeps the vendor inside the adapter
    (Section 5.1).
    """

    def __init__(
        self,
        config: PlaneConfig,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        resolver: SecretResolver | None = None,
    ) -> None:
        self.config = config
        if api_key is None:
            resolver = resolver or SecretResolver()
            api_key = resolver.resolve(
                SecretRef(name="plane_api_key", reference="env:PLANE_API_KEY", required=False)
            )
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    @property
    def has_credential(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise PlaneUnavailable("PLANE_API_KEY is not resolvable; projection is degraded")
        return {"x-api-key": self._api_key, "content-type": "application/json"}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.config.project_root}{path}"
        try:
            response = self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise PlaneUnavailable(f"{method} {url} failed at the transport layer: {exc}") from exc
        if response.status_code >= 500:
            raise PlaneUnavailable(f"{method} {url} returned {response.status_code}")
        if response.status_code in (401, 403):
            raise PlaneUnavailable(
                f"{method} {url} returned {response.status_code}: the Plane credential was rejected"
            )
        return response

    # -- reads -------------------------------------------------------------

    def health(self) -> bool:
        try:
            return self.request("GET", "/states/").status_code == 200
        except PlaneUnavailable:
            return False

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        """Parse a body that may legitimately be empty (204) or not JSON (405)."""
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {"results": parsed}

    def _list(self, path: str) -> list[dict[str, Any]]:
        response = self.request("GET", path)
        if response.status_code != 200:
            return []
        return list(self._json(response).get("results", []))

    def list_work_items(self, *, per_page: int = 100) -> list[dict[str, Any]]:
        return self._list(f"/work-items/?per_page={per_page}")

    def list_modules(self) -> list[dict[str, Any]]:
        return self._list("/modules/")

    def list_cycles(self) -> list[dict[str, Any]]:
        return self._list("/cycles/")

    def list_states(self) -> list[dict[str, Any]]:
        return self._list("/states/")

    def worklogs_available(self) -> bool:
        """Measured 2026-08-02: 404 'Worklog is not enabled for the project'.

        When it is off, the derived durations are rendered onto the work item
        instead. They are never dropped, and never replaced by an estimate.
        """
        try:
            return self.request("GET", "/total-worklogs/").status_code == 200
        except PlaneUnavailable:
            return False

    # -- writes (projection direction only) --------------------------------

    def find_by_external_id(self, collection: str, external_id: str) -> dict[str, Any] | None:
        """Resolve an object this adapter previously created.

        Two strategies because the API is not uniform: the filtered GET returns
        a single object for work items and modules, and is ignored by cycles.
        The client-side match is the fallback that makes the caller uniform.
        """
        try:
            response = self.request(
                "GET",
                f"/{collection}/?external_source={EXTERNAL_SOURCE}&external_id={external_id}",
            )
        except PlaneUnavailable:
            return None
        if response.status_code == 200:
            parsed = self._json(response)
            if "id" in parsed and parsed.get("external_id") == external_id:
                return parsed
            for candidate in parsed.get("results", []) or []:
                if (
                    candidate.get("external_id") == external_id
                    and candidate.get("external_source") == EXTERNAL_SOURCE
                ):
                    return candidate
        for candidate in self._list(f"/{collection}/"):
            if (
                candidate.get("external_id") == external_id
                and candidate.get("external_source") == EXTERNAL_SOURCE
            ):
                return candidate
        return None

    def upsert(self, collection: str, external_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create-or-update keyed on ``external_id``.

        Fast path: POST, and on the measured 409 take the existing id straight
        out of the conflict body -- no read-before-write, so a concurrent create
        cannot race it. Slow path, for the collections that reject a duplicate
        without naming it: resolve by external id, then PATCH.
        """
        payload = dict(body)
        payload.setdefault("external_source", EXTERNAL_SOURCE)
        payload["external_id"] = external_id
        if collection == "cycles":
            # Measured: the cycle serializer requires project_id in the body,
            # and rejects the shorter spelling `project`.
            payload.setdefault("project_id", self.config.project_id)

        response = self.request("POST", f"/{collection}/", json=payload)
        if response.status_code in (200, 201):
            return self._json(response)
        if response.status_code == 409:
            existing = self._json(response).get("id")
            if not existing:
                raise PlaneUnavailable(
                    f"POST /{collection}/ returned 409 without an id to update"
                )
            return self._patch(collection, existing, body)
        if response.status_code == 400:
            existing = self.find_by_external_id(collection, external_id)
            if existing is not None:
                return self._patch(collection, existing["id"], body)
        raise PlaneUnavailable(
            f"POST /{collection}/ returned {response.status_code}: {response.text[:200]}"
        )

    def _patch(self, collection: str, object_id: str, body: dict[str, Any]) -> dict[str, Any]:
        patch = self.request("PATCH", f"/{collection}/{object_id}/", json=body)
        if patch.status_code in (200, 201):
            return self._json(patch)
        raise PlaneUnavailable(
            f"PATCH /{collection}/{object_id}/ returned {patch.status_code}: {patch.text[:200]}"
        )

    def comment(self, work_item_id: str, html: str) -> dict[str, Any]:
        response = self.request(
            "POST", f"/work-items/{work_item_id}/comments/", json={"comment_html": html}
        )
        if response.status_code in (200, 201):
            return self._json(response)
        raise PlaneUnavailable(
            f"POST comment returned {response.status_code}: {response.text[:200]}"
        )

    def link(self, work_item_id: str, url: str, title: str) -> dict[str, Any]:
        response = self.request(
            "POST", f"/work-items/{work_item_id}/links/", json={"url": url, "title": title}
        )
        if response.status_code in (200, 201):
            return self._json(response)
        raise PlaneUnavailable(f"POST link returned {response.status_code}: {response.text[:200]}")

    def delete(self, collection: str, object_id: str) -> bool:
        return self.request("DELETE", f"/{collection}/{object_id}/").status_code in (200, 204)

    def delete_work_item(self, work_item_id: str) -> bool:
        return self.delete("work-items", work_item_id)

    def close(self) -> None:
        self._client.close()


def derived_worklog(timing: TimingRecord) -> dict[str, float | None]:
    """Section 9.8: durations derived from system events.

    Every key ``plane.yaml`` lists is present. Keys the control plane cannot
    derive yet (model-call and tool durations, which are span-level facts owned
    by the runtime) are ``None`` -- not zero, and never an estimate. ``None``
    reads as "not measured"; a zero would read as "measured, took no time".
    """
    derived = derived_durations(timing)
    return {key: derived.get(key) for key in DERIVED_DURATION_KEYS}


def _task_body(task: TaskRecord, *, snapshot: ControlPlaneSnapshot) -> dict[str, Any]:
    """One Plane work item from one Task. Aliases only, no protected content."""
    worklog = derived_worklog(task.timing)
    lines = [
        f"<p><b>State</b>: {task.state}</p>",
        f"<p><b>Workstream</b>: {task.workstream or 'unassigned'}</p>",
        f"<p><b>Requirements</b>: {', '.join(task.requirement_ids) or 'none linked'}</p>",
        f"<p><b>Depends on</b>: {', '.join(task.depends_on) or 'nothing'}</p>",
    ]
    if task.lease is not None:
        lines.append(
            f"<p><b>Held by</b>: {task.lease.holder_alias or 'unowned'} "
            f"(fence {task.lease.fence_token}, worktree {task.lease.worktree or 'n/a'}, "
            f"stale={task.lease.is_stale})</p>"
        )
    if task.typed_blocker:
        lines.append(f"<p><b>Typed blocker</b>: {task.typed_blocker}</p>")
    measured = {key: value for key, value in worklog.items() if value is not None}
    if measured:
        rendered = ", ".join(f"{key}={value:.1f}s" for key, value in measured.items())
        lines.append(f"<p><b>Derived durations</b> (system events only): {rendered}</p>")
    lines.append(
        f"<p><i>Projected from EFAH authoritative state, contract "
        f"{snapshot.project.contract_id}@{snapshot.project.contract_version}. "
        f"Plane is a projection, not project truth.</i></p>"
    )
    return {
        "name": f"[{task.task_id}] {task.title}"[:250],
        "description_html": "".join(lines),
        # Blocker -> work_item_with_blocked_state: Plane's state ids are
        # per-project, so the presentation is carried on priority + name rather
        # than by inventing a state id this adapter does not own.
        "priority": "urgent" if task.state in BLOCKED_STATES else "none",
    }


class PlaneProjection:
    """``ProjectionPort`` over Plane. One-way, read-only at the source.

    Construction refuses anything but a :class:`ReadOnlySource` when a source is
    supplied, so this object physically cannot reach a write method on the
    control plane.
    """

    #: Contract Section 4.1. Read by the composition verifier and by GATE-D2-11.
    may_mutate_authoritative_state: bool = False
    writes_flow: str = "terminusdb_to_plane_one_way"

    def __init__(
        self,
        config: PlaneConfig,
        *,
        client: PlaneClient | None = None,
        source: ReadOnlySource | None = None,
    ) -> None:
        if source is not None and not isinstance(source, ReadOnlySource):
            raise AuthoritativeMutationAttempted(
                "the Plane projection may only be given a dashboard.source.ReadOnlySource; "
                "plane.yaml sets may_mutate_authoritative_state: false"
            )
        self.config = config
        self.client = client or PlaneClient(config)
        self._source = source

    @classmethod
    def from_pack(cls, pack: Any, **kwargs: Any) -> "PlaneProjection":
        return cls(PlaneConfig.from_pack(pack), **kwargs)

    # -- port surface -------------------------------------------------------

    def is_available(self) -> bool:
        return self.client.health()

    def project(self, snapshot: ControlPlaneSnapshot) -> ProjectionResult:
        """Push one snapshot to Plane. Never raises on a Plane outage.

        Order matters: the payload is assembled and scanned for protected
        content *before* the first network call, so a leak is caught locally
        rather than after half of it is already published.
        """
        payload = self.build_payload(snapshot)
        assert_no_protected_content(payload, where="plane_projection")

        with efah_span(
            "plane.project",
            kind=SpanKindName.TOOL_CALL,
            correlation=Correlation(
                project_id=snapshot.project.project_id,
                run_id=snapshot.project.current_run_id or "projection",
                terminus_commit=snapshot.terminus_commit,
            ),
            attributes={"plane.workspace": self.config.workspace, "plane.mode": self.config.mode},
        ) as span:
            try:
                result = self._apply(payload, snapshot)
            except PlaneUnavailable as exc:
                span.set_attribute("plane.status", DEGRADED)
                LOGGER.warning("plane projection degraded: %s", exc)
                return ProjectionResult(
                    status=self.config.outage_state,
                    project_id=snapshot.project.project_id,
                    detail=str(exc),
                    errors=(str(exc),),
                    payload_preview={"work_items": len(payload["work_items"])},
                )
            span.set_attribute("plane.status", result.status)
            return result

    # -- payload ------------------------------------------------------------

    def build_payload(self, snapshot: ControlPlaneSnapshot) -> dict[str, Any]:
        """Render the snapshot into the Plane object shapes. Pure, no I/O.

        Being pure is what makes the protected-content scan meaningful and lets
        the mapping be tested without a network.
        """
        modules = sorted({task.workstream for task in snapshot.tasks if task.workstream})
        cycles = [
            {
                "external_id": f"milestone:{milestone.milestone_id}",
                "name": milestone.name[:250],
                "description": f"EFAH milestone {milestone.milestone_id} ({milestone.state})",
            }
            for milestone in snapshot.project.milestones
        ]

        work_items: list[dict[str, Any]] = []
        for task in snapshot.tasks:
            body = _task_body(task, snapshot=snapshot)
            work_items.append({"external_id": f"task:{task.task_id}", "body": body, "parent": None})
            # WorkUnit -> sub_work_item. Measured: there is no sub-work-items
            # endpoint; a sub item is a work item carrying `parent`.
            for unit in task.work_units:
                work_items.append(
                    {
                        "external_id": f"workunit:{unit.work_unit_id}",
                        "body": {
                            "name": f"[{unit.work_unit_id}] {unit.summary or 'work unit'}"[:250],
                            "description_html": f"<p><b>State</b>: {unit.state}</p>",
                            "priority": "urgent" if unit.state in BLOCKED_STATES else "none",
                        },
                        "parent_external_id": f"task:{task.task_id}",
                    }
                )

        # ReleaseCandidate -> work_item.
        if snapshot.release is not None:
            outstanding = sorted(
                set(snapshot.release.gates_required) - set(snapshot.release.gates_passed)
            )
            work_items.append(
                {
                    "external_id": f"release:{snapshot.release.release_id}",
                    "body": {
                        "name": f"[release] {snapshot.release.release_id}"[:250],
                        "description_html": (
                            f"<p><b>Ready</b>: {snapshot.release.ready}</p>"
                            f"<p><b>Gates outstanding</b>: {', '.join(outstanding) or 'none'}</p>"
                        ),
                        "priority": "high" if outstanding else "none",
                    },
                    "parent": None,
                }
            )

        # EvaluationRun -> work_item_property: verdict and counts only. No
        # holdout assertion, fixture, or mutant can reach this shape.
        evaluation_properties = [
            {
                "external_id": f"evaluation:{evaluation.evaluation_id}",
                "task_external_id": f"task:{evaluation.task_id}" if evaluation.task_id else None,
                "properties": {
                    "visible_verdict": str(evaluation.visible_verdict or "unknown"),
                    "visible_passed": evaluation.visible_passed,
                    "visible_total": evaluation.visible_total,
                    "hidden_suite_verdict": str(evaluation.hidden_suite_verdict or "unknown"),
                    "hidden_assertions_total": evaluation.hidden_assertions_total,
                    "hidden_assertions_failed": evaluation.hidden_assertions_failed,
                    "oracle_version": evaluation.oracle_version,
                },
            }
            for evaluation in snapshot.evaluations
        ]

        return {
            "project_id": snapshot.project.project_id,
            "contract": f"{snapshot.project.contract_id}@{snapshot.project.contract_version}",
            "modules": [
                {"external_id": f"workstream:{name}", "name": name[:250]} for name in modules
            ],
            "cycles": cycles,
            "work_items": work_items,
            "decisions": [self._decision_payload(d) for d in snapshot.decisions],
            "evaluation_properties": evaluation_properties,
            "mapping": PROJECTION_MAPPING,
        }

    @staticmethod
    def _decision_payload(decision: DecisionRecord) -> dict[str, Any]:
        """Decision -> work_item_comment_and_link."""
        return {
            "decision_id": decision.decision_id,
            "comment_html": (
                f"<p><b>{decision.title}</b> — {decision.outcome} "
                f"(contract {decision.contract_version}, by {decision.decided_by} "
                f"at {decision.decided_at})</p><p>{decision.rationale}</p>"
            ),
            "link": decision.link,
        }

    # -- apply --------------------------------------------------------------

    def _apply(self, payload: Mapping[str, Any], snapshot: ControlPlaneSnapshot) -> ProjectionResult:
        errors: list[str] = []
        attempted = 0
        modules = self._upsert_all("modules", payload["modules"], errors)
        cycles = self._upsert_all("cycles", payload["cycles"], errors)
        attempted += len(payload["modules"]) + len(payload["cycles"]) + len(payload["work_items"])

        created: dict[str, str] = {}
        work_items = 0
        # Parents first, so a sub work item can bind to the id of its parent in
        # the same pass rather than needing a second one.
        ordered = sorted(payload["work_items"], key=lambda item: "parent_external_id" in item)
        for item in ordered:
            body = dict(item["body"])
            parent_external = item.get("parent_external_id")
            if parent_external:
                parent_id = created.get(parent_external)
                if parent_id:
                    body["parent"] = parent_id
            try:
                result = self.client.upsert("work-items", item["external_id"], body)
            except PlaneUnavailable as exc:
                errors.append(str(exc))
                continue
            created[item["external_id"]] = result["id"]
            work_items += 1

        comments = 0
        links = 0
        for decision in payload["decisions"]:
            target = next(iter(created.values()), None)
            if target is None:
                break
            try:
                self.client.comment(target, decision["comment_html"])
                comments += 1
                if decision.get("link"):
                    self.client.link(target, decision["link"], decision["decision_id"])
                    links += 1
            except PlaneUnavailable as exc:
                errors.append(str(exc))

        succeeded = modules + cycles + work_items
        # A per-item failure is a partial projection and still counts as a
        # projection. Nothing landing at all is an outage, and outage_state is
        # what plane.yaml says to report -- never a project failure.
        status = self.config.outage_state if attempted and not succeeded else "projected"

        return ProjectionResult(
            status=status,
            project_id=snapshot.project.project_id,
            work_items_upserted=work_items,
            modules_upserted=modules,
            cycles_upserted=cycles,
            comments_posted=comments,
            links_posted=links,
            worklog_api_available=self.client.worklogs_available(),
            detail=(
                "projection applied; Plane sample data coexists with EFAH objects "
                f"(external_source={EXTERNAL_SOURCE})"
            ),
            errors=tuple(errors),
        )

    def _upsert_all(
        self, collection: str, items: Iterable[Mapping[str, Any]], errors: list[str]
    ) -> int:
        count = 0
        for item in items:
            body = {key: value for key, value in item.items() if key != "external_id"}
            try:
                self.client.upsert(collection, item["external_id"], body)
                count += 1
            except PlaneUnavailable as exc:
                errors.append(str(exc))
        return count

    # -- explicitly absent --------------------------------------------------

    def write_back(self, *args: Any, **kwargs: Any) -> None:
        """Exists only to fail loudly. Section 4.1: the flow is one-way."""
        raise AuthoritativeMutationAttempted(
            "Plane may not write to authoritative state. Controlled commands from Plane are "
            "REQUESTS that enter the normal gate path via the API "
            "(plane.yaml -> authority.controlled_commands_bypass_gates: false)."
        )
