"""GATE-D2-11 — Plane projection completeness.

Contract Sections 4.1, 9.8 and 11.6 · ``plane.yaml``. Four assertions, all four
executable today against the real projection stack: the read models in
:mod:`api.state`, the read-only handle in :mod:`dashboard.source`, the thirteen
view builders in :mod:`dashboard.projections`, the publication guard in
:mod:`dashboard.redaction`, and the Plane adapter in :mod:`integrations.plane`.

Three rules shaped every check here.

**A type is not a measurement.** :class:`~dashboard.views.DashboardProjection`
declares one required field per Section 11.6 view, so a projection that omits a
view cannot be constructed at all. That makes "all thirteen present" true by
construction -- and therefore not worth asserting on its own. A1 asserts what the
gate actually claims, that the thirteen are *populated*, against a control plane
seeded until every view has something to show, and it fails against a projection
that satisfies the type while carrying no rows.

**A guard that rejects everything guards nothing.** A3's scanner would look
perfect if it refused every payload, and A2's "authoritative state unchanged"
would hold for free if the projection pass did nothing. So each check also runs
the arm that must *not* fire: a clean payload publishes, a real projection pass
upserts real objects into a scripted Plane, and the same comparators are shown
firing when the property is genuinely false.

**Two subjects are simulated here, and the evidence says which.** The
authoritative store is
:class:`~api.adapters.control_plane_memory.InMemoryControlPlane` rather than
TerminusDB, and Plane is an ``httpx.MockTransport`` replaying the responses
:mod:`integrations.plane` measured against the live API on 2026-08-02 (409
carrying the existing id on a duplicate work item, 404 on worklogs). Everything
between them -- read models, read-only source, projections, redaction, payload
builder, upsert idiom -- is the real code path. What A2 proves is therefore that
*this* pass mutated no authoritative record and that no write method is reachable
from the projection at all; it is not a statement about a TerminusDB transaction
log, and the evidence says so rather than letting a reader infer it.

This lives outside :mod:`evaluation.checks` because it is a self-contained set
with its own seeding and transport machinery; :data:`CHECKS_D2_11` is what the
registry merges.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from api.adapters.control_plane_memory import InMemoryControlPlane
from api.state import (
    ControlPlaneSnapshot,
    DecisionRecord,
    DriftFindingRecord,
    EvaluationRecord,
    KnowledgeRecord,
    LeaseRecord,
    ModelRunRecord,
    ProjectRecord,
    ProvenanceEdge,
    ReleaseRecord,
    TaskRecord,
    TimingRecord,
    WorkUnitRecord,
)
from dashboard.projections import build_projection, derived_durations, project_from_source
from dashboard.redaction import ProtectedContentLeak, assert_no_protected_content
from dashboard.source import READ_METHODS, MutationAttemptedFromDashboard, ReadOnlySource
from dashboard.views import REQUIRED_VIEWS, DashboardProjection
from evaluation.gate_spec import AssertionSpec, GateSpec
from governance.envelope import content_hash
from governance.states import DriftFinding, OwnerInterrupt, ProjectState, TaskState, Verdict
from integrations.pack import load_pack
from integrations.plane import (
    DERIVED_DURATION_KEYS,
    AuthoritativeMutationAttempted,
    PlaneClient,
    PlaneConfig,
    PlaneProjection,
    derived_worklog,
)
from observability.identity import ProtectedIdentityLeak

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; ``ok`` and ``bad`` are the only runtime needs, and resolving them
# on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


# ===========================================================================
# What the checks measure against, declared rather than inferred
# ===========================================================================

#: For each Section 11.6 view, the fields that must carry content before that
#: view counts as populated.
#:
#: Declared here rather than derived from "any non-empty field", because most
#: views carry summary counters that are populated at zero -- ``open_count: 0``,
#: ``has_cycle: false`` -- and a rule that accepted those would call an empty
#: dashboard complete. Each entry names the row-bearing or identity field a human
#: opening that view has to see. The order follows Section 11.6.
REQUIRED_CONTENT: dict[str, tuple[str, ...]] = {
    "project_and_milestone_status": ("project_id", "state", "milestones", "task_state_counts"),
    "task_ledger_and_critical_path": ("rows", "critical_path"),
    "task_ownership_leases_worktrees_and_stale_sessions": ("rows",),
    "contract_and_requirement_traceability": ("contract_id", "rows"),
    "scope_drift_findings": ("rows",),
    "model_run_aliases_and_role_history": ("rows", "aliases_seen", "roles_seen"),
    "visible_and_hidden_evaluation_status": ("rows",),
    "oracle_health_and_mutant_results": ("rows",),
    "dependency_versions_and_impact_maps": ("rows", "impact_maps"),
    "knowledge_and_hard_gold_promotion_state": ("rows",),
    "provenance_graph": ("edges", "node_count"),
    "release_readiness": ("release_id", "gates_required"),
    "exact_typed_blocker_and_requested_owner_decision": ("project_state", "blockers"),
}

#: Section 9.8, restated independently of :mod:`dashboard.projections`.
#:
#: Each derived duration is the difference between two *system-event*
#: timestamps, in the order the projection may try them. A4 recomputes every
#: duration from this declaration with its own arithmetic and compares; a
#: projection that started reading something other than these fields -- an
#: estimate, a wall-clock guess, a constant -- would have to disagree with a
#: table it does not own. An empty tuple means "not derivable from control-plane
#: events", where the only honest value is ``None``.
EXPECTED_SPANS: dict[str, tuple[tuple[str, str], ...]] = {
    "queue_duration": (("queued_at", "claimed_at"),),
    "active_duration": (
        ("started_at", "candidate_submitted_at"),
        ("started_at", "completed_at"),
    ),
    "blocked_duration": (("blocked_at", "resumed_at"),),
    "model_call_duration": (),
    "tool_duration": (),
    "evaluation_duration": (("verification_started_at", "completed_at"),),
    "human_wait_duration": (("blocked_at", "resumed_at"),),
    "rework_duration": (("resumed_at", "candidate_submitted_at"),),
    "total_wall_clock": (("queued_at", "merged_at"), ("queued_at", "completed_at")),
}

#: Substrings that betray an agent estimate rather than a system event. A timing
#: field named after any of these is the failure A4 exists to catch: Section 9.8
#: says durations are measured, not reported.
ESTIMATE_MARKERS: tuple[str, ...] = (
    "estimate",
    "eta",
    "guess",
    "predicted",
    "forecast",
    "self_report",
    "reported_by_agent",
    "story_point",
    "points",
    "effort",
    "claimed_duration",
)

#: A *foreign* model identifier, used only as a probe for the identity scanner in
#: A3. No alias this project uses resolves to a vendor, and no real identity for
#: any EFAH role appears in this file or in the evidence it emits.
IDENTITY_PROBE = "run on llama-3-70b"

#: Names Section 5.1 keeps off the dashboard's reachable surface. Every one is a
#: real write method on :class:`InMemoryControlPlane` (or its private state), so
#: a wrapper that leaked one would leak something that actually mutates.
FORBIDDEN_WRITE_NAMES: tuple[str, ...] = (
    "import_project",
    "upsert_task",
    "upsert_evaluation",
    "upsert_model_run",
    "upsert_knowledge",
    "upsert_drift_finding",
    "record_decision",
    "record_contract_review",
    "record_run_request",
    "set_release",
    "set_project_state",
    "add_provenance",
    "_projects",
)

#: Everything sent to the scripted Plane carries this key, so no path here can
#: reach for a real ``PLANE_API_KEY``.
_PROBE_API_KEY = "gate-d2-11-scripted-transport"

#: One fully recorded task timeline. Held as data because A4 subtracts these
#: timestamps itself and compares the result with the projection's.
_PROBE_TIMESTAMPS: dict[str, str] = {
    "queued_at": "2026-08-02T05:00:00+00:00",
    "claimed_at": "2026-08-02T05:00:30+00:00",
    "started_at": "2026-08-02T05:01:00+00:00",
    "candidate_submitted_at": "2026-08-02T05:09:00+00:00",
    "verification_started_at": "2026-08-02T05:09:30+00:00",
    "completed_at": "2026-08-02T05:11:00+00:00",
    "merged_at": "2026-08-02T05:12:00+00:00",
}


# ===========================================================================
# Subjects
# ===========================================================================


def _seeded_control_plane(repo_root: Path) -> tuple[InMemoryControlPlane, str]:
    """A control plane carrying something for every one of the thirteen views.

    Deliberately uncached and rebuilt per check. A2 writes to a control plane on
    purpose -- that is how it proves its comparator can see a change -- and a
    shared instance would carry that mutation into A1's population report and
    A3's payload scan, where it would look like a defect in the projection.

    The pack import is the real one (:func:`integrations.pack.load_pack`), so the
    requirement rows, oracle health, dependency registry, milestones and release
    scaffolding below are owner facts read out of ``project-pack/``. Only the
    tasks, the model run, the knowledge item, the drift finding, the decision and
    the release candidate are seeded, because no workstream has pushed real ones
    into this control plane yet -- and the evidence records which is which rather
    than presenting the whole thing as observed state.
    """
    control_plane = InMemoryControlPlane()
    project = control_plane.import_project(
        pack_root=str(repo_root / "project-pack"),
        requested_by="gate-d2-11",
        correlation_id="GATE-D2-11",
    )
    project_id = project.project_id
    milestone_id = project.milestones[0].milestone_id if project.milestones else None

    control_plane.upsert_task(
        TaskRecord(
            task_id="TSK-D2-11-A",
            project_id=project_id,
            title="project the control plane into Plane",
            state=TaskState.RUNNING,
            milestone_id=milestone_id,
            workstream="dashboard",
            requirement_ids=("GATE-D2-11-A1",),
            on_critical_path=True,
            lease=LeaseRecord(
                lease_id="LEASE-D2-11-A",
                task_id="TSK-D2-11-A",
                holder_alias="implementer-i12",
                worktree="/wt/d2-11-a",
                fence_token=3,
                acquired_at=_PROBE_TIMESTAMPS["claimed_at"],
                expires_at="2026-08-02T05:30:00+00:00",
                last_heartbeat_at=_PROBE_TIMESTAMPS["started_at"],
            ),
            timing=TimingRecord(**_PROBE_TIMESTAMPS),
            work_units=(
                WorkUnitRecord(
                    work_unit_id="WU-D2-11-A1",
                    task_id="TSK-D2-11-A",
                    state=TaskState.RUNNING,
                    summary="render the thirteen views",
                ),
            ),
        )
    )
    # A blocked task, so view 13 has an exact typed blocker to show and view 3
    # has a stale session. Its timing carries the blocked/resumed pair and
    # nothing else, which is what makes A4's "a missing timestamp yields None"
    # arm a measurement rather than a hypothetical.
    control_plane.upsert_task(
        TaskRecord(
            task_id="TSK-D2-11-B",
            project_id=project_id,
            title="await an owner scope decision",
            state=TaskState.BLOCKED_OWNER_DECISION,
            milestone_id=milestone_id,
            workstream="dashboard",
            depends_on=("TSK-D2-11-A",),
            requirement_ids=("GATE-D2-11-A2",),
            typed_blocker="OWNER_SCOPE_DECISION: projection scope beyond Section 11.6",
            owner_interrupt=OwnerInterrupt.OWNER_SCOPE_DECISION,
            lease=LeaseRecord(
                lease_id="LEASE-D2-11-B",
                task_id="TSK-D2-11-B",
                holder_alias="implementer-i12",
                worktree="/wt/d2-11-b",
                fence_token=1,
                is_stale=True,
            ),
            timing=TimingRecord(
                queued_at="2026-08-02T05:00:00+00:00",
                blocked_at="2026-08-02T05:02:00+00:00",
                resumed_at="2026-08-02T05:06:00+00:00",
            ),
        )
    )
    control_plane.upsert_evaluation(
        project_id,
        EvaluationRecord(
            evaluation_id="EVAL-D2-11",
            task_id="TSK-D2-11-A",
            visible_verdict=Verdict.PASS,
            visible_passed=4,
            visible_total=4,
            hidden_suite_name="sealed",
            hidden_suite_verdict=Verdict.PASS,
            hidden_assertions_total=2,
            hidden_assertions_failed=0,
            oracle_version="1.0.0",
        ),
    )
    control_plane.upsert_model_run(
        project_id,
        ModelRunRecord(
            run_id="RUN-D2-11",
            task_id="TSK-D2-11-A",
            model_alias="implementer-i12",
            role="implementer",
            gateway_class="production",
            started_at=_PROBE_TIMESTAMPS["started_at"],
            duration_ms=1200,
            outcome="ok",
        ),
    )
    control_plane.upsert_knowledge(
        project_id,
        KnowledgeRecord(
            knowledge_id="K-D2-11",
            statement="Plane worklogs are disabled for this project; durations render on the item",
            tier="T5_INDEPENDENTLY_VERIFIED",
            evidence_refs=("tests/integration/test_plane_projection.py",),
        ),
    )
    control_plane.upsert_drift_finding(
        project_id,
        DriftFindingRecord(
            finding_id="DF-D2-11",
            finding_type=DriftFinding.UNLINKED_TASK,
            detail="a seeded finding, so the drift view has a row to render",
            subject="TSK-D2-11-B",
        ),
    )
    control_plane.add_provenance(
        project_id,
        ProvenanceEdge(
            source="task:TSK-D2-11-A",
            relation="produced",
            target="artifact:dashboard_projection",
            repository_commit="0" * 40,
            content_hash="sha256:" + "0" * 64,
        ),
    )
    control_plane.record_decision(
        DecisionRecord(
            decision_id="DEC-D2-11",
            title="Plane is a projection, never truth",
            outcome="ACCEPTED",
            decided_by="owner",
            decided_at=_PROBE_TIMESTAMPS["queued_at"],
            contract_version="1.1",
            rationale="one-way flow from authoritative state, per the plane.yaml authority block",
        )
    )
    control_plane.set_release(
        project_id,
        ReleaseRecord(
            release_id="RC-D2-11",
            candidate_commit="0" * 40,
            gates_required=("GATE-D2-11",),
            gates_passed=(),
            blocking_gate_ids=("GATE-D2-11",),
            ready=False,
        ),
    )
    return control_plane, project_id


def _empty_snapshot() -> ControlPlaneSnapshot:
    """A snapshot that satisfies every type and carries no content at all.

    This is the projection A1 must refuse: thirteen views, all present, all
    empty. Nothing in the schema stops it from being built, which is exactly why
    "all thirteen present" is not the property the gate wrote down.
    """
    return ControlPlaneSnapshot(
        project=ProjectRecord(
            project_id="EMPTY",
            name="empty",
            state=ProjectState.RUNNING,
            contract_id="EFAH-CONTRACT-001",
            contract_version="1.1",
        )
    )


class _ScriptedPlane:
    """Plane's measured behaviour, replayed without a network.

    Not a convenience fake: these are the responses :mod:`integrations.plane`
    documents from its 2026-08-02 probe of the live API -- 409 carrying the
    existing id on a duplicate work item, 404 on ``/total-worklogs/``. A
    happy-path stub would let the upsert idiom regress silently, and the upsert
    idiom is the only place this adapter writes anything.

    It is also the observation point A2 needs: ``objects`` is every object the
    projection created, so "authoritative state unchanged" can be reported
    alongside proof that the pass was not inert.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        parts = [segment for segment in request.url.path.split("/") if segment]
        collection = parts[-1]

        if request.method == "GET":
            if collection == "total-worklogs":
                return httpx.Response(
                    404, json={"message": "Worklog is not enabled for the project"}
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        value for (coll, _), value in self.objects.items() if coll == collection
                    ]
                },
            )

        if request.method == "POST":
            body = json.loads(request.content)
            key = (collection, str(body.get("external_id")))
            if key in self.objects:
                return httpx.Response(
                    409,
                    json={
                        "error": "Issue with the same external id and external source exists",
                        "id": self.objects[key]["id"],
                    },
                )
            record = dict(body)
            record["id"] = f"{collection}-{len(self.objects)}"
            self.objects[key] = record
            return httpx.Response(201, json=record)

        if request.method == "PATCH":
            for record in self.objects.values():
                if record["id"] == parts[-1]:
                    record.update(json.loads(request.content))
                    return httpx.Response(200, json=record)
            return httpx.Response(404, json={"error": "not found"})

        return httpx.Response(405)


def _plane(
    ctx: GateContext, *, source: ReadOnlySource | None = None
) -> tuple[PlaneProjection, _ScriptedPlane]:
    """A real :class:`PlaneProjection` over a scripted transport.

    The config is resolved from the owner's ``plane.yaml`` and
    ``environments.yaml``, so the workspace, project id and one-way authority
    flags under test are the declared ones. The transport is a mock and the key
    is a literal, so nothing here reads ``PLANE_API_KEY`` or opens a socket.
    """
    config = PlaneConfig.from_pack(load_pack(ctx.repo_root / "project-pack"))
    scripted = _ScriptedPlane()
    client = PlaneClient(
        config,
        api_key=_PROBE_API_KEY,
        client=httpx.Client(transport=httpx.MockTransport(scripted.handler)),
    )
    return PlaneProjection(config, client=client, source=source), scripted


def _authoritative_fingerprint(control_plane: InMemoryControlPlane, project_id: str) -> str:
    """A content hash over every authoritative record for one project.

    ``captured_at`` is dropped because the snapshot stamps it from the clock at
    read time: leaving it in would make two identical states hash differently and
    turn A2 into a test of ``datetime.now``. Every other field -- project, tasks,
    requirements, runs, evaluations, oracles, dependencies, knowledge,
    provenance, drift findings, decisions, release -- is inside the hash.
    """
    snapshot = control_plane.snapshot(project_id)
    if snapshot is None:
        return "sha256:absent"
    body = snapshot.model_dump(mode="json")
    body.pop("captured_at", None)
    return content_hash(body)


# ===========================================================================
# Shared predicates -- the same code decides both arms of each check
# ===========================================================================


def _content_size(value: Any) -> int:
    """How much a field carries. ``0`` means nothing a reader could look at.

    ``False`` and ``0`` are *not* content: a view whose only populated field is
    ``has_cycle: false`` is an empty view with a default sitting in it.
    """
    if value is None or value is False:
        return 0
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value)
    if isinstance(value, (int, float)):
        return 1 if value else 0
    return 1


def _population_report(projection: DashboardProjection) -> tuple[dict[str, Any], list[str]]:
    """Which of the thirteen views carry content, and which are empty shells.

    Both arms of A1 run this one function: the positive arm over the seeded
    projection, the negative control over a projection built from an empty
    snapshot. Sharing it is the point -- a control that exercised a different
    predicate from the verdict would prove nothing about the verdict.
    """
    dumped = projection.model_dump(mode="json")
    report: dict[str, Any] = {}
    unpopulated: list[str] = []
    for index, view_name in enumerate(REQUIRED_VIEWS, start=1):
        view = dumped.get(view_name)
        required = REQUIRED_CONTENT.get(view_name, ())
        if not isinstance(view, dict):
            report[view_name] = {"section_11_6_item": index, "present": False, "populated": False}
            unpopulated.append(view_name)
            continue
        sizes = {field: _content_size(view.get(field)) for field in required}
        empty = sorted(field for field, size in sizes.items() if not size)
        report[view_name] = {
            "section_11_6_item": index,
            "present": True,
            "required_content": list(required),
            "content_sizes": sizes,
            "empty_fields": empty,
            "populated": not empty,
        }
        if empty:
            unpopulated.append(view_name)
    return report, unpopulated


def _reachable_write_surface(source: Any) -> tuple[dict[str, Any], list[str]]:
    """Which forbidden names a dashboard-side handle can actually reach.

    Attribute lookup, not a naming convention: a name that resolves to something
    callable is reachable whatever any docstring says about it. *How* it fails to
    resolve is recorded too -- a name that is merely absent is unreachable by
    accident, and the next refactor can put it back.
    """
    attempts: dict[str, Any] = {}
    reachable: list[str] = []
    for name in FORBIDDEN_WRITE_NAMES:
        try:
            attribute = getattr(source, name)
        except MutationAttemptedFromDashboard as exc:
            attempts[name] = {
                "reachable": False,
                "raised": type(exc).__name__,
                "refused_for_the_expected_reason": True,
            }
        except AttributeError as exc:
            attempts[name] = {
                "reachable": False,
                "raised": type(exc).__name__,
                "refused_for_the_expected_reason": False,
            }
        else:
            attempts[name] = {"reachable": True, "resolved_to": type(attribute).__name__}
            reachable.append(name)
    return attempts, reachable


def _leak_probe(
    payload: Mapping[str, Any],
    mutate: Callable[[dict[str, Any]], None],
    *,
    label: str,
    where: str,
) -> dict[str, Any]:
    """Inject one class of protected content and record whether it was caught.

    The payload is deep-copied through JSON first, so a probe cannot contaminate
    the clean payload the positive arm depends on.
    """
    probe = json.loads(json.dumps(payload, default=str))
    mutate(probe)
    try:
        assert_no_protected_content(probe, where=where)
    except ProtectedContentLeak as exc:
        return {"caught": True, "raised": type(exc).__name__, "reason": exc.reason}
    except ProtectedIdentityLeak as exc:
        return {"caught": True, "raised": type(exc).__name__, "reason": f"field {exc.field}"}
    return {"caught": False, "probe": label}


def _estimate_findings(field_names: Sequence[str], *, where: str) -> list[str]:
    """Field names that would let an agent estimate onto a timing record.

    Run over the real :class:`~api.state.TimingRecord` in A4's positive arm and
    over a forged field set in its negative control, so the control proves this
    predicate detects an estimate rather than always answering "none".
    """
    findings: list[str] = []
    for name in field_names:
        lowered = name.lower()
        for marker in ESTIMATE_MARKERS:
            if marker in lowered:
                findings.append(f"{where}: field {name!r} names an agent estimate ({marker!r})")
                break
        else:
            if not lowered.endswith("_at"):
                findings.append(
                    f"{where}: field {name!r} is not a system-event timestamp (no _at suffix)"
                )
    return findings


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _recompute_durations(timing: TimingRecord) -> dict[str, float | None]:
    """Recompute every Section 9.8 duration from :data:`EXPECTED_SPANS`.

    The check's own arithmetic over the record's own timestamps. It shares no
    code with :func:`dashboard.projections.derived_durations`; the point of A4 is
    that the two agree, and a projection reading anything but these fields could
    not make them agree.
    """
    recomputed: dict[str, float | None] = {}
    for key, pairs in EXPECTED_SPANS.items():
        value: float | None = None
        for start_field, end_field in pairs:
            start = _parse(getattr(timing, start_field, None))
            end = _parse(getattr(timing, end_field, None))
            if start is None or end is None:
                continue
            candidate = max((end - start).total_seconds(), 0.0)
            value = candidate
            if candidate:
                break
        recomputed[key] = value
    return recomputed


def _duration_findings(
    observed: Mapping[str, float | None], expected: Mapping[str, float | None], *, where: str
) -> list[str]:
    """Where a projected duration is not the subtraction of two system events.

    Three distinct failures, named separately because they mean different
    things: a value where the events give nothing is a *guess*; a ``None`` where
    they give a number is a measurement dropped; a value that disagrees with the
    subtraction is a number from somewhere else entirely.
    """
    findings: list[str] = []
    for key, expected_value in expected.items():
        observed_value = observed.get(key)
        if expected_value is None and observed_value is not None:
            findings.append(
                f"{where}: {key} is {observed_value!r} where the system events on the record "
                "yield nothing to subtract; a projection that guesses is a projection that lies"
            )
        elif expected_value is not None and observed_value is None:
            findings.append(
                f"{where}: {key} is None where the system events give {expected_value}s"
            )
        elif (
            expected_value is not None
            and observed_value is not None
            and abs(observed_value - expected_value) > 1e-6
        ):
            findings.append(
                f"{where}: {key} is {observed_value}s, not the {expected_value}s that "
                "subtracting the two system-event timestamps gives"
            )
    return findings


# ===========================================================================
# A1 — all thirteen required dashboard views are populated
# ===========================================================================


def d2_11_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``view_presence_check`` → ``all_thirteen_present``.

    Presence is the cheap half and it is checked first: the thirteen names come
    from the owner's ``plane.yaml``, they must match :data:`REQUIRED_VIEWS` in
    order, and each must be a *required* field on ``DashboardProjection`` -- so
    omitting one raises rather than yielding a twelve-view dashboard.

    The half that carries the assertion is population. The projection is built
    through :func:`~dashboard.projections.project_from_source` from a control
    plane seeded until every view has something to show, and each view must carry
    its row-bearing field. The negative control is the projection an empty
    snapshot produces: it satisfies the type completely, all thirteen views are
    present, and the same predicate must report every one of them unpopulated.
    Without that arm this check would be reading the class definition.

    What it does *not* claim: that a live project must show rows in all thirteen.
    A project with no drift findings honestly shows an empty drift view. The
    claim is that the projection populates every view when authoritative state
    carries the data, and that an empty projection is detected as empty.
    """
    declared = [str(v) for v in (ctx.pack_yaml("plane.yaml").get("required_views") or [])]
    model_fields = DashboardProjection.model_fields
    missing_fields = [name for name in REQUIRED_VIEWS if name not in model_fields]
    optional_fields = [
        name
        for name in REQUIRED_VIEWS
        if name in model_fields and not model_fields[name].is_required()
    ]

    control_plane, project_id = _seeded_control_plane(ctx.repo_root)
    projection = project_from_source(ReadOnlySource(control_plane), project_id)
    if projection is None:
        return bad(
            ["project_from_source returned no projection for the seeded project"],
            {"gate_execution_log": {"project_id": project_id}},
        )

    # A projection missing one of the thirteen must not be constructible at all.
    partial_refused = False
    partial_detail = "a projection missing one of the thirteen views was constructed"
    try:
        DashboardProjection(
            **{
                key: value
                for key, value in projection.model_dump().items()
                if key != REQUIRED_VIEWS[0]
            }
        )
    except Exception as exc:  # pydantic ValidationError; deliberately broad
        partial_refused = True
        partial_detail = type(exc).__name__

    report, unpopulated = _population_report(projection)

    # Negative control: the same predicate over a projection that is complete by
    # type and empty in substance.
    control_report, control_unpopulated = _population_report(build_projection(_empty_snapshot()))

    # And the accessor: every named view resolves, and a name outside the
    # thirteen is refused rather than served.
    accessor_failures = [
        f"view({name!r}) is not resolvable"
        for name in REQUIRED_VIEWS
        if not _view_resolves(projection, name)
    ]
    unknown_refused = not _view_resolves(projection, "a_fourteenth_view_nobody_declared")

    findings: list[str] = []
    if len(REQUIRED_VIEWS) != 13:
        findings.append(f"REQUIRED_VIEWS names {len(REQUIRED_VIEWS)} views, not thirteen")
    if declared != list(REQUIRED_VIEWS):
        findings.append(
            f"plane.yaml declares {declared} while the projection names {list(REQUIRED_VIEWS)}"
        )
    findings.extend(f"{name} is not a field on DashboardProjection" for name in missing_fields)
    findings.extend(
        f"{name} is an optional field, so a projection can omit it" for name in optional_fields
    )
    findings.extend(
        f"{name} has no declared required content, so 'populated' is undefined for it"
        for name in REQUIRED_VIEWS
        if name not in REQUIRED_CONTENT
    )
    findings.extend(
        f"view {name} is present but carries nothing: {report[name].get('empty_fields')}"
        for name in unpopulated
    )
    findings.extend(accessor_failures)
    if not unknown_refused:
        findings.append("DashboardProjection.view served a name outside the thirteen")
    if not partial_refused:
        findings.append(partial_detail)
    if len(control_unpopulated) != len(REQUIRED_VIEWS):
        findings.append(
            "negative control did not fire: an empty projection reported "
            f"{len(REQUIRED_VIEWS) - len(control_unpopulated)} of thirteen views populated "
            f"({sorted(set(REQUIRED_VIEWS) - set(control_unpopulated))}); the predicate is "
            "measuring type presence, not content"
        )

    execution_log = {
        "check": a.method or "view_presence_check",
        "expected": a.expected,
        "required_views_declared_in_plane_yaml": declared,
        "required_views_in_the_projection": list(REQUIRED_VIEWS),
        "views_are_required_fields": not missing_fields and not optional_fields,
        "a_projection_missing_one_view_is_refused": {
            "refused": partial_refused,
            "detail": partial_detail,
        },
        "views": report,
        "views_populated": len(REQUIRED_VIEWS) - len(unpopulated),
        "views_unpopulated": unpopulated,
        "accessor_refuses_an_undeclared_view": unknown_refused,
        "built_through": "dashboard.projections.project_from_source(ReadOnlySource(...))",
        "what_the_subject_is": (
            "an InMemoryControlPlane seeded from the real project-pack import plus two tasks, "
            "a model run, a knowledge item, a drift finding, a decision and a release "
            "candidate. Requirement rows, oracle health, the dependency registry and the "
            "milestones are owner facts from the pack; the rest is seeded because no "
            "workstream has pushed real ones into this control plane yet."
        ),
        "what_populated_does_not_mean": (
            "that a live project must show rows in all thirteen. A project with zero drift "
            "findings honestly shows an empty drift view. What is proven is that the "
            "projection populates every view when authoritative state carries the data, and "
            "that an empty projection is detected as empty rather than counted as complete."
        ),
    }
    negative_control = {
        "probe": "build a projection from an empty snapshot and run the same population predicate",
        "why": (
            "DashboardProjection declares one required field per view, so 'all thirteen "
            "present' is true of any projection that exists at all -- including one with no "
            "rows in it. Unless the empty projection is reported unpopulated, this check is "
            "reading the class definition rather than the dashboard."
        ),
        "views_present_in_the_empty_projection": len(REQUIRED_VIEWS),
        "views_reported_unpopulated": control_unpopulated,
        "detector_fires": len(control_unpopulated) == len(REQUIRED_VIEWS),
        "per_view": control_report,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "projection_hash": content_hash(projection.model_dump(mode="json")),
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"all {len(REQUIRED_VIEWS)} Section 11.6 views are required fields and carry content "
        "from a seeded control plane, while a type-complete empty projection is reported "
        "unpopulated in every one of them",
    )


def _view_resolves(projection: DashboardProjection, name: str) -> bool:
    try:
        projection.view(name)
    except KeyError:
        return False
    return True


# ===========================================================================
# A2 — Plane cannot mutate authoritative state
# ===========================================================================


def d2_11_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``write_attempt_probe`` → ``authoritative_state_unchanged``.

    Four arms, because "cannot mutate" is a structural claim and a before/after
    comparison alone would only describe one run.

    1. The read-only handle: :data:`~dashboard.source.READ_METHODS` is the whole
       surface, and every real write method on the control plane raises
       :class:`~dashboard.source.MutationAttemptedFromDashboard` at attribute
       lookup -- so a projection cannot call one even by mistake.
    2. The seams refuse a writable source: ``PlaneProjection`` raises on a bare
       control plane and accepts the wrapper, ``project_from_source`` refuses the
       same way, and ``write_back`` exists only to fail.
    3. A full projection pass runs against the scripted Plane, and the
       authoritative fingerprint before and after is identical.
    4. The comparator is shown detecting a change: the same fingerprint is taken
       across a real write through the control plane's own write half.

    Arm 4 is what makes arm 3 mean anything. A hash that never differs is not
    evidence of immutability, and neither is a projection pass that did nothing
    -- so the number of objects the pass actually wrote into Plane is part of the
    verdict, not a footnote.
    """
    control_plane, project_id = _seeded_control_plane(ctx.repo_root)
    source = ReadOnlySource(control_plane)

    read_surface = sorted(READ_METHODS)
    unreachable_reads = [name for name in read_surface if not callable(getattr(source, name, None))]
    write_attempts, reachable_writes = _reachable_write_surface(source)
    try:
        source.injected = "x"  # type: ignore[attr-defined]
        setattr_refused = False
    except MutationAttemptedFromDashboard:
        setattr_refused = True

    projection, scripted = _plane(ctx, source=source)

    seams: dict[str, Any] = {"plane_projection_accepts_the_read_only_handle": True}
    try:
        PlaneProjection(projection.config, client=projection.client, source=control_plane)  # type: ignore[arg-type]
        seams["plane_projection_refuses_a_writable_source"] = False
    except AuthoritativeMutationAttempted as exc:
        seams["plane_projection_refuses_a_writable_source"] = True
        seams["refusal_detail"] = str(exc)[:160]
    try:
        projection.write_back()
        seams["write_back_raises"] = False
    except AuthoritativeMutationAttempted:
        seams["write_back_raises"] = True
    try:
        project_from_source(control_plane, project_id)  # type: ignore[arg-type]
        seams["project_from_source_refuses_a_writable_source"] = False
    except TypeError:
        seams["project_from_source_refuses_a_writable_source"] = True

    hash_before = _authoritative_fingerprint(control_plane, project_id)
    result = projection.project(control_plane.snapshot(project_id))
    hash_after = _authoritative_fingerprint(control_plane, project_id)
    objects_written_to_plane = len(scripted.objects)

    # Negative control: change authoritative state through the write half the
    # dashboard cannot see, and require the same comparator to notice.
    control_plane.upsert_task(
        TaskRecord(
            task_id="TSK-D2-11-NEGATIVE-CONTROL",
            project_id=project_id,
            title="a record written through the control plane's own write half",
            state=TaskState.PROPOSED,
        )
    )
    hash_after_real_write = _authoritative_fingerprint(control_plane, project_id)

    findings: list[str] = []
    findings.extend(f"declared read method {name!r} is not callable" for name in unreachable_reads)
    findings.extend(
        f"the dashboard can reach {name!r} on authoritative state" for name in reachable_writes
    )
    findings.extend(
        f"{name!r} was unreachable by accident (AttributeError), not refused by the wrapper"
        for name, record in write_attempts.items()
        if not record["reachable"] and not record["refused_for_the_expected_reason"]
    )
    if not setattr_refused:
        findings.append("attributes can be set on the read-only source")
    if not seams["plane_projection_refuses_a_writable_source"]:
        findings.append("PlaneProjection accepted a writable control plane as its source")
    if not seams["write_back_raises"]:
        findings.append("PlaneProjection.write_back did not raise")
    if not seams["project_from_source_refuses_a_writable_source"]:
        findings.append("project_from_source accepted a writable control plane")
    if hash_after != hash_before:
        findings.append(
            f"authoritative state changed across a projection pass: {hash_before} -> {hash_after}"
        )
    if objects_written_to_plane <= 0 or result.work_items_upserted <= 0:
        findings.append(
            "negative control failed: the projection pass wrote nothing to Plane, so "
            "'authoritative state unchanged' describes a pass that never happened "
            f"(status={result.status}, errors={list(result.errors)[:3]})"
        )
    if hash_after_real_write == hash_after:
        findings.append(
            "negative control failed: the fingerprint did not change after a genuine write "
            "through the control plane's write half, so it cannot detect a mutation"
        )

    execution_log = {
        "check": a.method or "write_attempt_probe",
        "expected": a.expected,
        "read_surface": read_surface,
        "read_surface_size": len(read_surface),
        "forbidden_write_names_probed": list(FORBIDDEN_WRITE_NAMES),
        "write_attempts": write_attempts,
        "setattr_refused": setattr_refused,
        "seams": seams,
        "projection_pass": {
            "status": result.status,
            "blocks_project": result.blocks_project,
            "work_items_upserted": result.work_items_upserted,
            "modules_upserted": result.modules_upserted,
            "cycles_upserted": result.cycles_upserted,
            "comments_posted": result.comments_posted,
            "objects_in_plane_after_the_pass": objects_written_to_plane,
            "http_calls": len(scripted.calls),
            "errors": list(result.errors)[:3],
        },
        "authoritative_fingerprint_before": hash_before,
        "authoritative_fingerprint_after": hash_after,
        "unchanged": hash_after == hash_before,
        "fingerprint_excludes": ["captured_at (stamped from the clock at read time)"],
        "how_the_stores_are_modelled": (
            "authoritative state is InMemoryControlPlane, not TerminusDB, and Plane is an "
            "httpx.MockTransport replaying the responses integrations.plane measured on "
            "2026-08-02. What is proven is that no write method is reachable from the "
            "projection and that a full pass left every authoritative record byte-identical; "
            "no TerminusDB transaction log was inspected."
        ),
    }
    negative_control = {
        "probe": (
            "write a task through the control plane's own write half -- the half the dashboard "
            "cannot reach -- and re-take the same fingerprint"
        ),
        "why": (
            "'unchanged' is what a broken comparator says about everything, and it is also "
            "what a projection pass that did nothing would leave behind. This arm shows the "
            "comparator detects a real mutation; the pass counters show the pass published."
        ),
        "fingerprint_after_a_real_write": hash_after_real_write,
        "comparator_detects_the_change": hash_after_real_write != hash_after,
        "objects_the_projection_wrote_to_plane": objects_written_to_plane,
        "pass_was_not_inert": objects_written_to_plane > 0,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "authoritative_fingerprint_before": hash_before,
            "authoritative_fingerprint_after": hash_after,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"the projection reaches {len(read_surface)} read methods and none of "
        f"{len(FORBIDDEN_WRITE_NAMES)} write names; a full pass published "
        f"{objects_written_to_plane} objects to Plane and left the authoritative fingerprint "
        "unchanged, while the same fingerprint moved for a genuine write",
    )


# ===========================================================================
# A3 — no holdout assertion, private fixture, or mutant source appears in Plane
# ===========================================================================


def d2_11_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``projection_content_scan`` → ``zero_protected_content``.

    Scanned at both publication points, because they are different surfaces:
    :func:`~dashboard.projections.build_projection` runs the guard on the
    rendered thirteen views, and :meth:`PlaneProjection.project` runs it on the
    assembled payload *before the first network call*. The second is asserted as
    an ordering property -- a leak caught after half the payload is already
    published is not caught -- by counting the HTTP calls the scripted transport
    saw.

    Then the negative controls: one per class the gate names, plus the classes
    ``plane.yaml -> protected_content_rule`` adds. A holdout assertion field, a
    private fixture, mutant source, assertion body text in a value, and a real
    model identifier. Each must raise, and the raised type is recorded.

    Which is why the clean arm is load-bearing, and why the size of the clean
    subjects is part of the verdict: the untouched projection and the untouched
    payload must publish, and both must be non-trivial. A scanner that rejects
    everything satisfies "zero protected content" perfectly and publishes
    nothing; a scanner that passes an empty dict has demonstrated nothing.
    """
    control_plane, project_id = _seeded_control_plane(ctx.repo_root)
    snapshot = control_plane.snapshot(project_id)
    if snapshot is None:
        return bad(["the seeded control plane returned no snapshot"])

    projection, scripted = _plane(ctx, source=ReadOnlySource(control_plane))
    payload = projection.build_payload(snapshot)
    rendered = build_projection(snapshot).model_dump(mode="json")

    clean: dict[str, Any] = {}
    for label, subject, where in (
        ("rendered_projection", rendered, "dashboard_projection"),
        ("plane_payload", payload, "plane_projection"),
    ):
        try:
            assert_no_protected_content(subject, where=where)
            clean[label] = {"published": True}
        except (ProtectedContentLeak, ProtectedIdentityLeak) as exc:
            clean[label] = {"published": False, "raised": type(exc).__name__, "detail": str(exc)}

    payload_size = {
        "work_items": len(payload.get("work_items", [])),
        "modules": len(payload.get("modules", [])),
        "cycles": len(payload.get("cycles", [])),
        "decisions": len(payload.get("decisions", [])),
        "evaluation_properties": len(payload.get("evaluation_properties", [])),
    }
    view_rows = sum(
        len(view.get("rows", ())) for view in rendered.values() if isinstance(view, dict)
    )

    def _inject(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(probe: dict[str, Any]) -> None:
            probe["work_items"][0]["body"][field] = value

        return mutate

    # Every injection lands on a real work item, because that is where Plane
    # content lives. If the payload carries none, the guard was never exercised
    # and saying so is the honest result -- not an empty probe set reported as
    # "nothing was caught, and nothing needed to be".
    probes: dict[str, dict[str, Any]] = {}
    if payload.get("work_items"):
        injections: dict[str, Any] = {
            "holdout_assertion": ("holdout_assertion", "the sealed suite expects 42"),
            "private_fixture": ("private_fixture", {"input": 7, "expect": 42}),
            "mutant_source": ("mutant_source", "def seeded_mutant(x):\n    return not x\n"),
            "assertion_body_in_a_value": (
                "description_html",
                "<p>the mutant patch was: assert result == 42</p>",
            ),
            "real_model_identity": ("name", IDENTITY_PROBE),
        }
        probes = {
            label: _leak_probe(
                payload, _inject(field, value), label=label, where="plane_projection"
            )
            for label, (field, value) in injections.items()
        }
    uncaught = sorted(name for name, record in probes.items() if not record["caught"])

    # The ordering property, on the real project() path rather than on a bare
    # scan: a leaking snapshot must be refused before any network call. The leak
    # is planted in a decision rationale because that is a field the payload
    # genuinely carries -- a probe planted somewhere Plane never sees would be
    # caught by nothing and prove nothing. (Drift findings, for instance, do not
    # cross into Plane at all; they are used for the dashboard arm below.)
    plane_leaking_snapshot = ControlPlaneSnapshot(
        project=snapshot.project,
        decisions=(
            DecisionRecord(
                decision_id="DEC-D2-11-LEAK",
                title="leak probe",
                outcome="probe",
                decided_by="gate-d2-11",
                decided_at=_PROBE_TIMESTAMPS["queued_at"],
                contract_version="1.1",
                rationale="the mutant patch was: assert result == 42",
            ),
        ),
        release=snapshot.release,
    )
    calls_before = len(scripted.calls)
    try:
        projection.project(plane_leaking_snapshot)
        pre_flight: dict[str, Any] = {"refused": False}
    except (ProtectedContentLeak, ProtectedIdentityLeak) as exc:
        pre_flight = {
            "refused": True,
            "raised": type(exc).__name__,
            "network_calls_made": len(scripted.calls) - calls_before,
        }

    # And the same class of content at the dashboard publication point, where a
    # drift finding's detail does render into one of the thirteen views.
    dashboard_leaking_snapshot = ControlPlaneSnapshot(
        project=snapshot.project,
        drift_findings=(
            DriftFindingRecord(
                finding_id="DF-D2-11-LEAK",
                finding_type=DriftFinding.PROTECTED_ASSET_ACCESS,
                detail="the mutant patch was: assert result == 42",
            ),
        ),
        release=snapshot.release,
    )
    try:
        build_projection(dashboard_leaking_snapshot)
        projection_layer: dict[str, Any] = {"refused": False}
    except (ProtectedContentLeak, ProtectedIdentityLeak) as exc:
        projection_layer = {"refused": True, "raised": type(exc).__name__}

    identity_snapshot = ControlPlaneSnapshot(
        project=snapshot.project,
        knowledge=(
            KnowledgeRecord(knowledge_id="K-LEAK", statement=IDENTITY_PROBE, tier="T2_HYPOTHESIS"),
        ),
    )
    try:
        build_projection(identity_snapshot)
        identity_layer: dict[str, Any] = {"refused": False}
    except ProtectedIdentityLeak as exc:
        identity_layer = {"refused": True, "raised": type(exc).__name__, "field": exc.field}
    except ProtectedContentLeak as exc:
        identity_layer = {"refused": True, "raised": type(exc).__name__, "reason": exc.reason}

    findings: list[str] = []
    findings.extend(
        f"negative control failed: the {label} was rejected by the guard "
        f"({record.get('raised')}: {str(record.get('detail'))[:120]}). A scanner that refuses "
        "clean content is not a leak check"
        for label, record in clean.items()
        if not record["published"]
    )
    if payload_size["work_items"] <= 0 or view_rows <= 0:
        findings.append(
            "the clean subjects are empty, so passing the scan proves nothing "
            f"(payload {payload_size}, view rows {view_rows})"
        )
    if not probes:
        findings.append(
            "the payload carries no work item to inject into, so the guard was never "
            "exercised against protected content at all"
        )
    findings.extend(
        f"protected content not caught in the Plane payload: {name}" for name in uncaught
    )
    if not pre_flight["refused"]:
        findings.append("a leaking snapshot was projected to Plane without being refused")
    elif pre_flight.get("network_calls_made"):
        findings.append(
            f"the leak was caught after {pre_flight['network_calls_made']} network call(s); "
            "the guard must run before the first one"
        )
    if not projection_layer["refused"]:
        findings.append("the dashboard projection published assertion text")
    if not identity_layer["refused"]:
        findings.append("the dashboard projection published a real model identity")

    execution_log = {
        "check": a.method or "projection_content_scan",
        "expected": a.expected,
        "scanned_surfaces": {
            "dashboard_projection": "build_projection runs the guard on the rendered thirteen views",
            "plane_payload": "PlaneProjection.project runs the guard before the first network call",
        },
        "clean_subjects_publish": clean,
        "clean_payload_size": payload_size,
        "clean_projection_row_count": view_rows,
        "protected_content_rule": ctx.pack_yaml("plane.yaml").get("protected_content_rule"),
        "leak_caught_before_the_first_network_call": pre_flight,
        "dashboard_layer_refuses_assertion_text": projection_layer,
        "dashboard_layer_refuses_a_model_identity": identity_layer,
        "what_is_not_proven": (
            "that no protected content exists anywhere upstream. The guard decides what may "
            "be published, so what is proven is that these two publication points refuse it "
            "-- by field name, by value shape, and by identity scan -- not that the sealed "
            "side is intact."
        ),
    }
    negative_control = {
        "probe": (
            "inject each protected class into the Plane payload in turn -- a holdout assertion "
            "field, a private fixture, mutant source, assertion body text, and a real model "
            "identifier -- and require the guard to raise for each"
        ),
        "why": (
            "a guard that rejected every payload would satisfy 'zero protected content' "
            "perfectly and publish nothing. The injections show it fires; the clean arm shows "
            "it fires only on the injection."
        ),
        "injections": probes,
        "injections_buildable": bool(probes),
        "all_caught": bool(probes) and not uncaught,
        "clean_arm": clean,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "payload_hash": content_hash(payload),
            "projection_hash": content_hash(rendered),
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"a {payload_size['work_items']}-work-item payload and a {view_rows}-row projection "
        f"both publish, while all {len(probes)} injected classes of protected content are "
        "refused -- the snapshot-level leak before any network call was made",
    )


# ===========================================================================
# A4 — worklog durations derive from system events, not agent estimates
# ===========================================================================


def d2_11_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``worklog_source_assert`` → ``source == system_events``.

    The schema is the first half. :class:`~api.state.TimingRecord` carries ten
    fields, every one an ``_at`` system-event timestamp, none named after an
    estimate, and ``extra='forbid'`` means an adapter cannot add one -- so
    "durations derive from system events" is not a convention somebody has to
    keep. The refusal of an injected ``estimated_hours`` is measured rather than
    assumed.

    The arithmetic is the second half, and it is done twice. This check
    recomputes every duration from :data:`EXPECTED_SPANS` with its own
    subtraction and compares against what the projection produced -- for a fully
    recorded task, for one whose timeline is half written, and for a record with
    no timestamps at all. A missing timestamp must yield ``None``: not ``0.0``,
    which reads as "measured, took no time", and not a guess. The numbers the
    Plane work item renders are then required to be those same values, because a
    projection could derive honestly and publish something else.

    The negative controls run the same two predicates over subjects where the
    property is false: a field set carrying an estimate, and a duration table
    with a number where the events give nothing.
    """
    worklog_policy = ctx.pack_yaml("plane.yaml").get("worklog") or {}
    timing_fields = list(TimingRecord.model_fields)
    schema_findings = _estimate_findings(timing_fields, where="api.state.TimingRecord")

    estimate_refused = False
    estimate_detail = "TimingRecord accepted an estimated_hours field"
    try:
        TimingRecord(estimated_hours=3)  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError; deliberately broad
        estimate_refused = True
        estimate_detail = type(exc).__name__

    control_plane, project_id = _seeded_control_plane(ctx.repo_root)
    snapshot = control_plane.snapshot(project_id)
    complete = snapshot.task("TSK-D2-11-A") if snapshot else None
    partial = snapshot.task("TSK-D2-11-B") if snapshot else None
    if snapshot is None or complete is None or partial is None:
        return bad(
            ["the seeded snapshot does not carry the two probe tasks"],
            {
                "gate_execution_log": {
                    "tasks": [t.task_id for t in snapshot.tasks] if snapshot else []
                }
            },
        )

    subjects = {
        "fully_recorded_task": complete.timing,
        "half_recorded_task": partial.timing,
        "no_timestamps_at_all": TimingRecord(),
    }
    measurements: dict[str, Any] = {}
    duration_findings: list[str] = []
    computed_values = 0
    for label, timing in subjects.items():
        observed = derived_durations(timing)
        expected = _recompute_durations(timing)
        worklog = derived_worklog(timing)
        duration_findings.extend(_duration_findings(observed, expected, where=label))
        duration_findings.extend(
            _duration_findings(
                {key: worklog.get(key) for key in expected}, expected, where=f"{label}/worklog"
            )
        )
        computed_values += sum(1 for value in observed.values() if value is not None)
        measurements[label] = {
            "timestamps_present": sorted(
                name for name in timing_fields if getattr(timing, name, None)
            ),
            "observed": observed,
            "recomputed_by_this_check": expected,
            "worklog": worklog,
            "keys_returning_none": sorted(key for key, value in worklog.items() if value is None),
        }

    # The rendered work item must carry the numbers that were computed.
    plane_projection, _ = _plane(ctx, source=ReadOnlySource(control_plane))
    payload = plane_projection.build_payload(snapshot)
    rendered_item = next(
        (item for item in payload["work_items"] if item["external_id"] == "task:TSK-D2-11-A"), None
    )
    rendered_html = str((rendered_item or {}).get("body", {}).get("description_html", ""))
    measured = {
        key: value for key, value in derived_worklog(complete.timing).items() if value is not None
    }
    unrendered = [
        f"{key}={value:.1f}s"
        for key, value in measured.items()
        if f"{key}={value:.1f}s" not in rendered_html
    ]

    # Ledger rows carry three of the durations; they must be the same numbers.
    ledger_row = next(
        row
        for row in build_projection(snapshot).task_ledger_and_critical_path.rows
        if row.task_id == "TSK-D2-11-A"
    )
    expected_complete = _recompute_durations(complete.timing)
    row_findings = _duration_findings(
        {
            "active_duration": ledger_row.active_duration_seconds,
            "blocked_duration": ledger_row.blocked_duration_seconds,
            "total_wall_clock": ledger_row.total_wall_clock_seconds,
        },
        {
            key: expected_complete[key]
            for key in ("active_duration", "blocked_duration", "total_wall_clock")
        },
        where="task_ledger_row",
    )

    # Negative control 1: the estimate detector, over a forged field set.
    forged_fields = [*timing_fields, "estimated_hours", "agent_reported_effort"]
    control_schema_findings = _estimate_findings(forged_fields, where="forged_timing_record")
    # Negative control 2: the guess detector, over a table that fabricates a
    # value everywhere the system events give nothing.
    honest = _recompute_durations(partial.timing)
    guessed = {key: (value if value is not None else 900.0) for key, value in honest.items()}
    control_duration_findings = _duration_findings(guessed, honest, where="guessing_projection")

    findings: list[str] = [*schema_findings, *duration_findings, *row_findings]
    if len(timing_fields) != 10:
        findings.append(
            f"TimingRecord carries {len(timing_fields)} fields, not the ten system events "
            f"Section 9.8 names: {timing_fields}"
        )
    if not estimate_refused:
        findings.append(estimate_detail)
    if computed_values <= 0:
        findings.append(
            "no duration was computed at all, so 'derived from system events' is vacuous"
        )
    if worklog_policy.get("source") != "system_events_only":
        findings.append(f"plane.yaml worklog.source is {worklog_policy.get('source')!r}")
    if worklog_policy.get("agent_estimates_permitted") is not False:
        findings.append("plane.yaml does not forbid agent estimates")
    if list(DERIVED_DURATION_KEYS) != list(worklog_policy.get("derived_durations") or []):
        findings.append(
            f"the adapter's duration keys {list(DERIVED_DURATION_KEYS)} differ from plane.yaml's "
            f"{list(worklog_policy.get('derived_durations') or [])}"
        )
    if sorted(EXPECTED_SPANS) != sorted(DERIVED_DURATION_KEYS):
        findings.append(
            "this check's span table does not cover the adapter's duration keys: "
            f"{sorted(set(EXPECTED_SPANS) ^ set(DERIVED_DURATION_KEYS))}"
        )
    if unrendered:
        findings.append(
            f"the projected work item does not carry the computed durations: {unrendered}"
        )
    if "system events only" not in rendered_html:
        findings.append("the projected work item does not state that the durations are derived")
    if not control_schema_findings:
        findings.append(
            "negative control did not fire: a field set containing 'estimated_hours' was "
            "reported free of agent estimates"
        )
    if not control_duration_findings:
        findings.append(
            "negative control did not fire: a table fabricating durations where the system "
            "events give none was reported consistent"
        )

    execution_log = {
        "check": a.method or "worklog_source_assert",
        "expected": a.expected,
        "timing_record_fields": timing_fields,
        "every_field_is_a_system_event_timestamp": not schema_findings,
        "estimate_field_refused_by_the_schema": {
            "refused": estimate_refused,
            "detail": estimate_detail,
        },
        "plane_yaml_worklog_policy": worklog_policy,
        "duration_keys": list(DERIVED_DURATION_KEYS),
        "span_table_used_by_this_check": {
            key: [list(pair) for pair in pairs] for key, pairs in EXPECTED_SPANS.items()
        },
        "measurements": measurements,
        "durations_computed": computed_values,
        "task_ledger_row": {
            "task_id": ledger_row.task_id,
            "active_duration_seconds": ledger_row.active_duration_seconds,
            "blocked_duration_seconds": ledger_row.blocked_duration_seconds,
            "total_wall_clock_seconds": ledger_row.total_wall_clock_seconds,
        },
        "rendered_durations_present_on_the_work_item": not unrendered,
        "keys_that_are_never_derivable_here": [
            key for key, pairs in EXPECTED_SPANS.items() if not pairs
        ],
        "why_none_and_not_zero": (
            "model-call and tool durations are span-level facts the control plane does not "
            "hold. None reads as 'not measured'; 0.0 would read as 'measured, took no time', "
            "and that is the shape an estimate takes when it is dressed as a measurement."
        ),
        "observed_quirk": (
            "derived_durations selects between two candidate spans with `or`, so a span of "
            "exactly 0.0 seconds falls through to the next candidate. Both candidates are "
            "system events, so the provenance this assertion is about is unaffected; it is "
            "recorded rather than left for a reader to rediscover."
        ),
    }
    negative_control = {
        "probe": (
            "run the same two predicates over subjects where the property is false: a timing "
            "field set carrying 'estimated_hours' and 'agent_reported_effort', and a duration "
            "table that fills every underivable value with 900.0"
        ),
        "why": (
            "'no estimate field' and 'no guessed duration' are what a predicate that never "
            "fires says about everything. These arms show both detectors firing, and for the "
            "reason claimed rather than incidentally."
        ),
        "estimate_detector_findings": control_schema_findings,
        "estimate_detector_fires": bool(control_schema_findings),
        "guess_detector_findings": control_duration_findings,
        "guess_detector_fires": bool(control_duration_findings),
        "honest_table_for_the_half_recorded_task": honest,
    }
    evidence = {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"TimingRecord carries {len(timing_fields)} system-event timestamps and no estimate "
        f"field the schema would accept; {computed_values} durations recompute exactly from "
        "those timestamps, and a missing timestamp yields None rather than a number",
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D2_11: dict[tuple[str, str], Check] = {
    ("GATE-D2-11", "A1"): d2_11_a1,
    ("GATE-D2-11", "A2"): d2_11_a2,
    ("GATE-D2-11", "A3"): d2_11_a3,
    ("GATE-D2-11", "A4"): d2_11_a4,
}
