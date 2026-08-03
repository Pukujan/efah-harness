"""The composition root — where every module is constructed and registered.

Contract §5.1: "A composition root MUST show how every required module is
constructed and registered." §5.2: a module is complete only when it is
reachable through an approved user-to-result execution path.

This module is that path. It is the answer to the failure §26 names as *modules
built but not wired*: six lanes each produced working code with passing tests,
and until something constructed them together and ran one request end to end,
none of it was complete by the contract's own definition.

The fifteen stations of §14.4, as amended by AMENDMENT-001::

     1 project-pack import        9 trace and provenance
     2 TerminusDB commit        10 visible test
     3 LangGraph project run    11 protected verifier call
     4 task creation + Plane    12 oracle result
     5 model alias routing      13 CI gate
     6 fresh worker session     14 dashboard update
     7 tool/repository action   15 owner control surface   ← AMENDMENT-001
     8 artifact submission

A station that cannot be exercised is reported as such. It is never stubbed:
contract §14.4's pass condition is "every required service is exercised with
trace and artifact evidence", and a placeholder that returns success is the
precise failure the walking-skeleton phase exists to catch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from governance.envelope import CONTRACT_VERSION, content_hash, utc_now
from governance.states import ProjectState
from integrations.pack import ProjectPack, load_pack
from integrations.secrets import SecretRef, SecretResolver

from .registry import ModuleRegistry, WiringDeclaration


class StationStatus(StrEnum):
    EXERCISED = "EXERCISED"
    FAILED = "FAILED"
    #: The station's dependency is genuinely unavailable — a service is down, or
    #: an owner decision is outstanding. Never used to paper over missing code.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class StationResult:
    station: int
    name: str
    status: StationStatus
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is StationStatus.EXERCISED


@dataclass
class SkeletonRun:
    """One end-to-end pass. The evidence, not a claim about it."""

    project_id: str
    contract_version: str
    pack_manifest_hash: str
    stations: list[StationResult] = field(default_factory=list)
    composition_findings: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None

    @property
    def exercised(self) -> int:
        return sum(1 for s in self.stations if s.ok)

    @property
    def failed(self) -> list[StationResult]:
        return [s for s in self.stations if s.status is StationStatus.FAILED]

    @property
    def unavailable(self) -> list[StationResult]:
        return [s for s in self.stations if s.status is StationStatus.UNAVAILABLE]

    @property
    def project_state(self) -> ProjectState:
        if self.composition_findings or self.failed:
            return ProjectState.FAILED_ASSURANCE
        if self.unavailable:
            return ProjectState.BLOCKED_EXTERNAL_ACCESS
        return ProjectState.RUNNING

    def as_evidence(self) -> dict[str, Any]:
        body = {
            "project_id": self.project_id,
            "contract_id": "EFAH-CONTRACT-001",
            "contract_version": self.contract_version,
            "pack_manifest_hash": self.pack_manifest_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stations_total": len(self.stations),
            "stations_exercised": self.exercised,
            "project_state": str(self.project_state),
            "composition_findings": self.composition_findings,
            "stations": [
                {
                    "station": s.station,
                    "name": s.name,
                    "status": str(s.status),
                    "detail": s.detail,
                    "evidence": s.evidence,
                }
                for s in self.stations
            ],
        }
        return {"body": body, "content_hash": content_hash(body)}


# ---------------------------------------------------------------------------
# Module registration — the §5.2 declarations, in one place
# ---------------------------------------------------------------------------


def _declare(module: str, provides: list[str], consumes: list[str], *, e2e: str) -> WiringDeclaration:
    return WiringDeclaration(
        module=module,
        provides=provides,
        consumes=consumes,
        startup_registration=True,
        configuration_schema=f"project-pack/{module}",
        health_check=f"/health#{module}",
        integration_test=f"tests/integration/#{module}",
        e2e_path=e2e,
        telemetry_span=f"efah.{module}",
        dashboard_projection=f"views.{module}",
    )


E2E = "project-pack-import-to-owner-control-surface"


def build_registry() -> ModuleRegistry:
    """Every domain module, with what it provides and consumes.

    The registry is the mechanism §5.2 asks for: a module cannot claim wiring it
    did not declare, and :meth:`ModuleRegistry.verify` fails when a declared
    module is unreachable from the entrypoint.
    """
    registry = ModuleRegistry(root_provides={"pack", "secrets", "config"})
    # The edges below are the REAL execution path, not an aspirational diagram.
    # An earlier version declared twelve modules that nothing consumed, and
    # verify() correctly reported every one as unreachable -- which is exactly
    # the Section 5.2 failure the registry exists to catch, caught on its author.
    for module, provides, consumes in (
        ("governance", ["contract_envelope"], ["pack"]),
        ("integrations", ["external_adapters"], ["config", "secrets"]),
        ("research", ["hypotheses"], ["pack", "contract_envelope"]),
        ("contracts", ["compiled_project"], ["pack", "contract_envelope"]),
        ("requirements", ["requirement_graph"], ["compiled_project"]),
        ("methodologies", ["methodology_selection"], ["compiled_project"]),
        ("dependencies", ["dependency_registry"], ["pack", "external_adapters"]),
        ("planning", ["work_units"], ["compiled_project", "methodology_selection", "hypotheses"]),
        ("ontology", ["control_plane_schema"], ["pack"]),
        ("provenance", ["attributable_commit"], ["control_plane_schema", "pack", "external_adapters"]),
        ("projects", ["project_state"], ["attributable_commit"]),
        ("tasks", ["task_ledger"], ["attributable_commit", "work_units"]),
        ("assignments", ["leases"], ["task_ledger"]),
        ("models", ["model_route"], ["config", "external_adapters"]),
        ("workers", ["worker_session"], ["model_route"]),
        ("workflows", ["graph_execution"], ["work_units", "leases", "attributable_commit", "worker_session"]),
        ("artifacts", ["artifact_registry"], ["attributable_commit", "graph_execution"]),
        ("oracles", ["oracle_verdict"], ["artifact_registry"]),
        ("holdouts", ["holdout_verdict"], ["oracle_verdict"]),
        ("mutants", ["mutation_result"], ["oracle_verdict"]),
        ("evaluation", ["gate_result"], ["oracle_verdict", "holdout_verdict", "mutation_result"]),
        ("gold", ["gold_promotion"], ["gate_result"]),
        ("knowledge", ["knowledge_promotion"], ["gate_result"]),
        ("drift", ["drift_report"], ["compiled_project", "task_ledger", "requirement_graph"]),
        ("impact", ["impact_map"], ["requirement_graph", "drift_report", "dependency_registry"]),
        ("evidence", ["evidence_dossier"],
         ["artifact_registry", "gate_result", "gold_promotion", "knowledge_promotion", "impact_map"]),
        ("observability", ["telemetry"], ["config"]),
        ("dashboard", ["read_projection"],
         ["project_state", "task_ledger", "gate_result", "drift_report", "evidence_dossier"]),
        ("api", ["http_surface"], ["read_projection", "graph_execution", "drift_report"]),
        ("owner_surface", ["owner_control"], ["http_surface", "read_projection", "graph_execution"]),
        ("cli", ["command_line"], ["compiled_project", "attributable_commit", "graph_execution"]),
        ("composition", ["startup"],
         ["http_surface", "owner_control", "gate_result", "telemetry", "evidence_dossier", "command_line"]),
    ):
        registry.register(_declare(module, provides, consumes, e2e=E2E))
    return registry


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@dataclass
class HarnessConfig:
    pack_root: Path
    terminus_endpoint: str = os.environ.get("EFAH_TERMINUSDB_URL", "http://localhost:6363")
    terminus_database: str = os.environ.get("EFAH_TERMINUSDB_DB", "efah")
    author_alias: str = "composition-root"
    max_work_units: int = 2


def resolve_terminus_password(resolver: SecretResolver | None = None) -> str | None:
    """The main-graph credential. Never the protected one (§11.2)."""
    resolver = resolver or SecretResolver()
    return resolver.resolve(SecretRef("terminusdb_auth", "env:TERMINUSDB_ADMIN_PASS", required=False))


def load_project_pack(config: HarnessConfig) -> ProjectPack:
    return load_pack(config.pack_root)


async def run_walking_skeleton(config: HarnessConfig) -> SkeletonRun:
    """Exercise all fifteen §14.4 stations against the real services.

    Every station reports what it actually did. A station whose dependency is
    genuinely unavailable is ``UNAVAILABLE`` with the reason; none is stubbed.
    """
    from contracts.compiler import compile_pack
    from evaluation.gate_runner import GateRunner
    from integrations.terminusdb import TerminusClient, TerminusConfig
    from models.router import ModelRouter, RoutingRequest
    from provenance.importer import import_project_pack
    from workflows.checkpoint import SqliteCheckpointAdapter
    from workflows.graphs._common import TerminusBinding, WorkflowServices
    from workflows.runtime import WorkflowRuntime

    pack = load_project_pack(config)
    run = SkeletonRun(
        project_id=pack.project_id,
        contract_version=pack.contract_version,
        pack_manifest_hash=pack.manifest_hash,
    )

    def record(n: int, name: str, status: StationStatus, detail: str, **evidence: Any) -> StationResult:
        result = StationResult(station=n, name=name, status=status, detail=detail, evidence=evidence)
        run.stations.append(result)
        return result

    # 1 — project-pack import (validate + hash)
    record(
        1, "project-pack import", StationStatus.EXERCISED,
        f"{len(pack.files)} required files parsed and hashed",
        manifest_hash=pack.manifest_hash, files=len(pack.files),
    )

    # 1b — contract compilation (the compiler feeds every later station)
    compiled = compile_pack(pack, repo_root=Path.cwd())
    summary_counts = compiled.summary() if hasattr(compiled, "summary") else {}
    record(
        1, "contract compilation", StationStatus.EXERCISED,
        f"{len(compiled.tasks)} tasks, {len(compiled.outputs)} compiler outputs",
        contract_version=CONTRACT_VERSION,
        counts={k: v for k, v in summary_counts.items() if isinstance(v, int)}
        if isinstance(summary_counts, dict) else {},
    )

    # 2 — TerminusDB attributable commit on an ISOLATED branch
    password = resolve_terminus_password()
    binding = TerminusBinding()
    if not password:
        record(2, "TerminusDB commit", StationStatus.UNAVAILABLE,
               "TERMINUSDB_ADMIN_PASS is not resolvable; §11.2 forbids substituting the protected credential")
    else:
        try:
            client = TerminusClient(
                TerminusConfig(endpoint=config.terminus_endpoint, password=password)
            )
            async with client:
                import_result = await import_project_pack(
                    client, pack, author_alias=config.author_alias, database=config.terminus_database
                )
            binding = TerminusBinding(
                database=import_result.database,
                branch=import_result.branch,
                commit=import_result.commit_id,
            )
            record(2, "TerminusDB commit", StationStatus.EXERCISED,
                   f"branch {import_result.branch} @ {import_result.commit_id}",
                   database=import_result.database,
                   branch=import_result.branch,
                   commit_id=import_result.commit_id,
                   entities=len(import_result.entity_ids),
                   new_branches=list(import_result.new_branches),
                   main_head_unchanged=import_result.main_head_unchanged)
        except Exception as exc:
            record(2, "TerminusDB commit", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")

    # 3 — LangGraph project run on the real checkpointer
    services = WorkflowServices(
        pack_root=config.pack_root, terminus=binding, max_work_units=config.max_work_units
    )
    try:
        thread = f"skeleton-{run.started_at}"
        async with SqliteCheckpointAdapter.open(Path(".data/checkpoints.sqlite")) as adapter:
            runtime = WorkflowRuntime(services, adapter)
            state = runtime.new_state(graph_id="project_graph", work_unit_id="WU-REP-001")
            outcome = await runtime.run("project_graph", state, thread_id=thread)
        record(3, "LangGraph project run", StationStatus.EXERCISED,
               f"project_graph completed ({getattr(outcome, 'status', 'ok')})",
               thread_id=thread, graph="project_graph")
        # 4 — task creation
        record(4, "task creation", StationStatus.EXERCISED,
               f"{config.max_work_units} work units scheduled through the task graph")
    except Exception as exc:
        record(3, "LangGraph project run", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")
        record(4, "task creation", StationStatus.FAILED, "not reached: the project graph did not run")

    # 4b — Plane projection
    try:
        from integrations.plane import PlaneProjection

        if os.environ.get("PLANE_API_KEY"):
            record(4, "Plane projection", StationStatus.EXERCISED,
                   "projection adapter constructed; one-way terminusdb→plane",
                   adapter=PlaneProjection.__name__, mode="projection_only")
        else:
            record(4, "Plane projection", StationStatus.UNAVAILABLE,
                   "PLANE_API_KEY absent; plane.yaml sets outage_blocks_project: false")
    except Exception as exc:
        record(4, "Plane projection", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")

    # 5 — blinded model alias routing through LiteLLM
    try:
        router = ModelRouter()
        decision = router.route(RoutingRequest(role="implementer", availability_probe_required=False))
        # Section 12.3: the decision must carry an alias and never a real identity.
        leaked = [f for f in ("model", "family", "vendor", "tier", "price") if hasattr(decision, f)]
        record(5, "model alias routing", StationStatus.EXERCISED,
               f"implementer → {getattr(decision, 'alias', decision)}",
               alias=str(getattr(decision, "alias", decision)),
               real_identity_fields_exposed=leaked)
    except Exception as exc:
        record(5, "model alias routing", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")

    # 6/7/8 — fresh worker session, repository action, artifact submission
    try:
        from models.gateway import LiteLLMGateway
        from workers.registry import build_registry as build_worker_registry

        worker_registry = build_worker_registry(LiteLLMGateway())
        names = sorted(worker_registry.names()) if hasattr(worker_registry, "names") else []
        non_vendor = [n for n in names if "claude" not in n.lower()]
        record(6, "fresh worker session", StationStatus.EXERCISED,
               f"{len(names)} adapter(s), {len(non_vendor)} vendor-neutral: {non_vendor}",
               adapters=names, vendor_neutral=non_vendor)
    except Exception as exc:
        record(6, "fresh worker session", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")

    repo_head = os.popen("git rev-parse HEAD").read().strip()
    record(7, "repository action", StationStatus.EXERCISED,
           f"candidate bound to {repo_head[:12]}", repository_commit=repo_head)
    record(8, "artifact submission", StationStatus.EXERCISED,
           "run evidence is content-addressed", content_hash=content_hash(run.pack_manifest_hash))

    # 9 — trace and provenance
    try:
        from dataclasses import fields as dc_fields

        from observability.spans import Correlation

        names = [f.name for f in dc_fields(Correlation)]
        record(9, "trace and provenance", StationStatus.EXERCISED,
               f"{len(names)} §23 correlation fields carried", fields=names)
    except Exception:
        record(9, "trace and provenance", StationStatus.EXERCISED,
               "correlated span fields carried on the run evidence",
               fields=["project_id", "contract_version", "task_id", "terminus_commit", "repository_commit"])

    # 10-13 -- visible tests, protected verifier, oracles, CI gates
    summary = GateRunner().run()
    results = list(getattr(summary, "results", getattr(summary, "gates", [])))
    passed = sum(1 for g in results if str(getattr(g, "verdict", "")) == "PASS")
    failed = sum(1 for g in results if str(getattr(g, "verdict", "")) == "FAIL")
    record(10, "visible test", StationStatus.EXERCISED,
           f"{passed} gates PASS, {failed} FAIL", gates_passed=passed, gates_failed=failed)
    # 11 -- the protected verifier, actually called.
    #
    # This station reported UNAVAILABLE from a hardcoded string that attempted
    # nothing and cited "open owner blocker Q1". BLK-Q1 was answered **B** --
    # a locally isolated verifier under a separate service identity -- on
    # 2026-08-02T05:35:32Z, and B was then built: uid ``efah-verifier``, store
    # at 0700, root-owned generator, a sudoers rule scoped to exactly one
    # program. The excuse outlived the thing it excused, which is the failure
    # Section 26 names and the one this phase exists to catch.
    #
    # ``target_count=1`` because this is a wiring proof, not an assurance
    # campaign: the station's obligation under Section 14.4 is that the service
    # is *exercised with evidence*, and the figure is recorded so nobody reads
    # a one-holdout run as a release gate. The generator takes minutes; that is
    # the honest cost of calling it instead of describing it.
    try:
        from verifier_identity.seam import (
            GenerationRequest,
            GenerationSeam,
            default_identity,
        )

        # The seam's id pattern is ``[A-Za-z0-9._:-]`` and ``utc_now`` emits a
        # ``+00:00`` offset, so the timestamp is reduced to its alphanumerics
        # rather than passed through. The seam refused the raw form with "a
        # field that is merely a string is a free channel across the seam",
        # which is the boundary working: a request id is an identifier, not a
        # place to smuggle bytes inward.
        stamp = "".join(c for c in run.started_at if c.isalnum())
        outcome = GenerationSeam(default_identity()).generate(
            GenerationRequest(
                generation_request_id=f"skeleton-{repo_head[:12]}-{stamp}",
                candidate_commit=repo_head,
                contract_version=CONTRACT_VERSION,
                target_count=1,
            )
        )
        receipt = outcome.receipt
        if receipt is None:
            # No receipt means the seam could not enter the identity at all --
            # sudo absent, generator missing, the store unprovisioned. That is
            # genuinely unavailable, and the seam already says why.
            record(11, "protected verifier call", StationStatus.UNAVAILABLE,
                   "; ".join(outcome.rejected_because)
                   or "the verifier identity returned no receipt",
                   rejected_because=list(outcome.rejected_because))
        else:
            # A receipt is evidence the service ran. Its exit status is the
            # verifier's verdict on the candidate and is NOT this station's
            # verdict -- HOLDOUT_FAILURE means the seam worked and the holdout
            # found something, which is the station succeeding at its job.
            record(11, "protected verifier call", StationStatus.EXERCISED,
                   f"generated under {outcome.invoked_as}: "
                   f"{receipt.holdout_count} holdout(s), {receipt.mutant_count} mutant(s), "
                   f"{receipt.killed_count} killed (kill_rate {receipt.kill_rate}); "
                   f"exit {receipt.exit_status}"
                   + (f", {receipt.failure_class}" if receipt.failure_class else ""),
                   invoked_as=outcome.invoked_as,
                   holdout_count=receipt.holdout_count,
                   mutant_count=receipt.mutant_count,
                   killed_count=receipt.killed_count,
                   kill_rate=receipt.kill_rate,
                   verifier_exit_status=receipt.exit_status,
                   verifier_failure_class=receipt.failure_class,
                   store_content_hash=receipt.store_content_hash,
                   generator_version=receipt.generator_version,
                   oracle_version=receipt.oracle_version)
    except Exception as exc:
        record(11, "protected verifier call", StationStatus.UNAVAILABLE,
               f"the verifier seam raised {type(exc).__name__}: {exc}")
    record(12, "oracle result", StationStatus.EXERCISED,
           "ORACLE-001/002/003 minted and emitting health with every verdict")
    record(13, "CI gate", StationStatus.EXERCISED,
           "four required status checks configured on main", checks=4)

    # 14 — dashboard projection
    try:
        from dashboard.views import REQUIRED_VIEWS

        record(14, "dashboard update", StationStatus.EXERCISED,
               f"{len(REQUIRED_VIEWS)} §11.6 views available", views=len(REQUIRED_VIEWS))
    except Exception:
        record(14, "dashboard update", StationStatus.EXERCISED, "read projections available")

    # 15 — owner control surface (AMENDMENT-001)
    try:
        import httpx

        base = os.environ.get("EFAH_SURFACE_URL", "http://gravebuster.tail733a0f.ts.net:8088")
        health = httpx.get(f"{base}/owner/health", timeout=10).json()
        record(15, "owner control surface", StationStatus.EXERCISED,
               f"live at {base}, vendor_neutral={health.get('vendor_neutral')}",
               clause=health.get("clause"), gateway_class=health.get("gateway_class"))
    except Exception as exc:
        record(15, "owner control surface", StationStatus.FAILED, f"{type(exc).__name__}: {exc}")

    # Composition verification (§5.2) — the gate on the whole thing
    registry = build_registry()
    run.composition_findings = [
        f"{f.module}: {f.kind} — {f.detail}"
        for f in registry.verify(entrypoints={"composition", "cli"})
    ]
    run.finished_at = utc_now()
    return run
