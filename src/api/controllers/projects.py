"""Project use cases (contract Sections 6.1, 9.6, 11.5, 19.1)."""

from __future__ import annotations

from api.context import RequestContext
from api.errors import NotFound, PackValidationRejected, ScopeExpansionRejected
from api.ports import ControlPlaneReadPort, ControlPlaneWritePort, DriftEnginePort, RuntimePort
from api.state import GraphView, ProjectRecord, RunHandle
from dashboard.projections import build_projection
from dashboard.source import ReadOnlySource
from dashboard.views import DashboardProjection, ScopeDriftFindings
from governance.states import DriftFinding
from integrations.pack import PackValidationError
from observability.spans import Correlation, SpanKindName, efah_span


class ProjectController:
    """Import, run, status, graph, and scope-drift use cases.

    The dashboard-facing reads go through :class:`ReadOnlySource` rather than
    the raw control plane: the status and drift views are read projections
    (Section 5.1), and routing them through the read-only handle means the
    controller *cannot* accidentally mutate while rendering one.
    """

    def __init__(
        self,
        *,
        reader: ControlPlaneReadPort,
        writer: ControlPlaneWritePort,
        runtime: RuntimePort,
        drift_engine: DriftEnginePort,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._runtime = runtime
        self._drift = drift_engine
        self._read_only = ReadOnlySource(reader)

    # ---------------------------------------------------------------- commands

    def import_project(self, *, pack_root: str, context: RequestContext) -> ProjectRecord:
        """Section 6.1 steps 1-2. A bad pack is a typed blocker, not a 500."""
        with efah_span(
            "project.import",
            kind=SpanKindName.PROJECT,
            correlation=Correlation(run_id=context.request_id, project_id="pending"),
            attributes={"pack_root": pack_root},
        ) as span:
            try:
                project = self._writer.import_project(
                    pack_root=pack_root,
                    requested_by=context.principal.subject,
                    correlation_id=context.correlation_id,
                )
                # The project id is not knowable until the pack parses, so the
                # span opens as "pending" and is corrected here rather than
                # leaving a span that names no project.
                span.set_attribute("efah.project_id", project.project_id)
                span.set_attribute("efah.pack_manifest_hash", project.pack_manifest_hash or "")
                return project
            except PackValidationError as exc:
                # Section 8.1: no silent defaults. The pack is rejected whole.
                raise PackValidationRejected(
                    f"project pack failed validation and was not imported: {exc}"
                ) from exc

    def run(self, *, project_id: str, context: RequestContext, reason: str = "") -> RunHandle:
        project = self._reader.get_project(project_id)
        if project is None:
            raise NotFound(f"project {project_id} has not been imported")

        # Section 19.1: a run request carrying an instruction is compared to the
        # approved contract *before* anything executes. Rejected, never executed.
        if reason:
            finding = self._drift.classify_instruction(project_id=project_id, instruction=reason)
            if finding is not None:
                raise ScopeExpansionRejected(finding.detail, finding_type=str(finding.finding_type))

        with efah_span(
            "project.run",
            kind=SpanKindName.PROJECT,
            correlation=Correlation(project_id=project_id, run_id=context.request_id),
        ):
            return self._runtime.start_project_run(
                project_id=project_id,
                requested_by=context.principal.subject,
                correlation_id=context.correlation_id,
            )

    # ------------------------------------------------------------------ queries

    def status(self, *, project_id: str) -> DashboardProjection:
        """All thirteen Section 11.6 views for this project."""
        snapshot = self._read_only.snapshot(project_id)
        if snapshot is None:
            raise NotFound(f"project {project_id} has not been imported")
        graph = self._read_only.graph(project_id)
        impact_maps = {}
        for dependency in snapshot.dependencies:
            impact = self._read_only.impact_map(dependency.dependency_id)
            if impact is not None:
                impact_maps[dependency.dependency_id] = impact.model_dump(mode="json")
        return build_projection(
            snapshot,
            critical_path=graph.critical_path if graph else (),
            has_cycle=graph.has_cycle if graph else False,
            impact_maps=impact_maps,
        )

    def graph(self, *, project_id: str) -> GraphView:
        view = self._read_only.graph(project_id)
        if view is None:
            raise NotFound(f"project {project_id} has not been imported")
        return view

    def scope_drift(self, *, project_id: str) -> ScopeDriftFindings:
        if self._reader.get_project(project_id) is None:
            raise NotFound(f"project {project_id} has not been imported")
        findings = self._drift.findings(project_id)
        rows = tuple(
            {
                "finding_id": finding.finding_id,
                "finding_type": finding.finding_type,
                "detail": finding.detail,
                "subject": finding.subject,
                "detected_at": finding.detected_at,
                "resolved": finding.resolved,
            }
            for finding in findings
        )
        by_type: dict[str, int] = {}
        for row in rows:
            by_type[str(row["finding_type"])] = by_type.get(str(row["finding_type"]), 0) + 1
        return ScopeDriftFindings(
            rows=rows,  # type: ignore[arg-type]
            open_count=sum(1 for row in rows if not row["resolved"]),
            by_type=by_type,
        )


class ContractDriftEngine:
    """Default ``DriftEnginePort``: findings recorded on the snapshot, plus a
    conservative instruction classifier.

    The classifier is deliberately *deny-by-keyword* rather than model-judged.
    Contract Section 17.3 keeps model judges out of deterministic verdict paths,
    and "may this instruction change the scope of the build?" is a verdict.
    A human-written instruction that trips it is rejected with
    ``UNAPPROVED_SCOPE_EXPANSION`` and can be re-issued as a contract amendment,
    which is the path Section 1.3 already defines.
    """

    #: Verbs and objects that describe changing the contract rather than
    #: executing it. Compared against a normalised instruction.
    SCOPE_EXPANDING_PHRASES = (
        "add a new feature",
        "also build",
        "additionally build",
        "change the contract",
        "amend the contract",
        "ignore the contract",
        "skip the gate",
        "skip gates",
        "bypass the gate",
        "disable the gate",
        "weaken the",
        "relax the requirement",
        "drop the requirement",
        "remove the requirement",
        "out of scope but",
        "beyond the contract",
        "new workstream",
        "expand scope",
        "widen the scope",
    )

    def __init__(self, reader: ControlPlaneReadPort) -> None:
        self._reader = reader

    def findings(self, project_id: str):
        snapshot = self._reader.snapshot(project_id)
        if snapshot is None:
            return []
        return list(snapshot.drift_findings)

    def classify_instruction(self, *, project_id: str, instruction: str):
        from api.state import DriftFindingRecord

        normalised = " ".join(instruction.lower().split())
        for phrase in self.SCOPE_EXPANDING_PHRASES:
            if phrase in normalised:
                return DriftFindingRecord(
                    finding_id=f"DRIFT-{abs(hash((project_id, phrase))) % 10**8:08d}",
                    finding_type=DriftFinding.UNAPPROVED_SCOPE_EXPANSION,
                    detail=(
                        f"instruction matches scope-expanding phrase {phrase!r}; contract "
                        "Section 1.3 requires an owner-approved amendment, not an instruction"
                    ),
                    subject=project_id,
                )
        return None
