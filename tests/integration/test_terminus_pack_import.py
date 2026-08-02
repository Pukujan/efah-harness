"""GATE-D1-01 and the task ledger, end to end against the live graph.

The import runs into a scratch database rather than ``efah`` so the test is
repeatable, but the code path is the same one that created ``efah`` -- nothing is
stubbed and nothing is skipped for the test's convenience.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from governance.envelope import CONTRACT_ID, Envelope
from governance.states import ProjectState, TaskState
from integrations.pack import load_pack
from integrations.terminusdb import MAIN_BRANCH, TerminusClient, TerminusConfig
from ontology.schema import (
    Artifact,
    Contract,
    Dependency,
    LeaseState,
    ModelAlias,
    Project,
    Task,
    TaskEventType,
    WorkUnit,
)
from projects import ProjectRepository, TerminalStateViolation
from provenance import ProvenanceWriter, import_project_pack
from provenance.binding import assert_fully_bound, verify_entity
from tasks import Actor, ActorKind, LedgerAuthorityViolation, StaleLeaseRejected, TaskService

PACK_ROOT = Path(__file__).resolve().parents[2] / "project-pack"
AUTHOR = "ws-b-terminusdb"
MAIN_ENDPOINT = "http://localhost:6363"


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the live TerminusDB tests need it")
    return value


@pytest_asyncio.fixture
async def live_client() -> AsyncIterator[TerminusClient]:
    password = require_env("TERMINUSDB_ADMIN_PASS")
    client = TerminusClient(TerminusConfig(endpoint=MAIN_ENDPOINT, password=password))
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def scratch_database(live_client: TerminusClient) -> AsyncIterator[str]:
    name = f"efah_test_{uuid.uuid4().hex[:10]}"
    await live_client.ensure_database(name, label="EFAH test", comment="integration test scratch")
    try:
        yield name
    finally:
        try:
            await live_client.delete_database(name)
        except Exception:  # pragma: no cover - cleanup must not mask a failure
            pass


@pytest.fixture
def pack():
    return load_pack(PACK_ROOT)


async def test_import_creates_an_isolated_branch_and_leaves_main_alone(
    live_client: TerminusClient, scratch_database: str, pack, tmp_path: Path
):
    """GATE-D1-01 A1 and A3, measured rather than asserted about."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )

    assert result.branch != MAIN_BRANCH
    assert result.new_branch_present
    assert result.branches_before == (MAIN_BRANCH,)
    assert result.main_head_unchanged
    assert result.is_isolated
    assert result.commit_id

    # A3: attributable and immutable.
    assert result.receipt.is_attributable
    assert result.receipt.commit_record is not None
    assert result.receipt.commit_record.is_immutable
    assert result.receipt.author_alias == AUTHOR

    # The evidence bundle GATE-D1-01 asks for, serialisable and hashed.
    evidence = result.as_evidence()
    assert evidence["before_after_branch_listing"]["main_head_unchanged"] is True
    assert evidence["terminusdb_branch_name_and_commit_id"]["commit_id"] == result.commit_id
    assert len(evidence["import_log_with_file_manifest_and_hashes"]) == 11
    (tmp_path / "gate-d1-01.json").write_text(json.dumps(evidence, indent=2))
    assert json.loads((tmp_path / "gate-d1-01.json").read_text())["gate_id"] == "GATE-D1-01"


async def test_imported_contract_matches_the_pack(
    live_client: TerminusClient, scratch_database: str, pack
):
    """GATE-D1-01 A4."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    contract = await writer.read(Contract, "EFAH-CONTRACT-001")
    assert isinstance(contract, Contract)
    assert contract.contract_key == CONTRACT_ID == pack.contract_id
    assert contract.current_version == pack.contract_version


async def test_every_imported_entity_is_fully_provenance_bound(
    live_client: TerminusClient, scratch_database: str, pack
):
    """GATE-D1-02 A1 and A4 against what is actually stored."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    for model in (Project, Contract, Dependency, ModelAlias):
        stored = await writer.read_all(model)
        assert stored, f"no {model.__name__} survived the import"
        for entity in stored:
            assert_fully_bound(entity)
            assert entity.envelope.terminus_database == scratch_database
            assert entity.envelope.terminus_branch == result.branch
            assert entity.envelope.terminus_commit == result.commit_id
            assert verify_entity(entity), f"{entity.document_id} content hash does not verify"


async def test_no_real_model_identity_reaches_the_main_graph(
    live_client: TerminusClient, scratch_database: str, pack
):
    """GATE-D1-06: the stored aliases carry no vendor and no model id."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    policy = pack.yaml("model-policy.yaml")
    secrets = {str(b["litellm_model"]) for b in policy["aliases"].values()} | {
        str(b["family"]) for b in policy["aliases"].values()
    }
    documents = await live_client.get_documents(
        scratch_database, branch=result.branch, doc_type="ModelAlias"
    )
    assert len(documents) == len(policy["aliases"])
    blob = json.dumps(documents)
    assert not [s for s in secrets if s in blob]


async def test_dependency_edges_are_stored_with_real_referential_integrity(
    live_client: TerminusClient, scratch_database: str, pack
):
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    edges = await writer.read_all(Dependency)
    assert edges
    known = set(result.entity_ids)
    for edge in edges:
        assert edge.source in known and edge.target in known
    assert {str(e.edge_type) for e in edges} >= {"derived_from", "depends_on", "evaluated_by"}


async def test_gate_evidence_artifact_is_recorded_in_the_graph(
    live_client: TerminusClient, scratch_database: str, pack
):
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    artifacts = [a for a in await writer.read_all(Artifact) if a.artifact_type == "gate_evidence"]
    assert artifacts, "GATE-D1-01 evidence must be bound into the graph, not only returned"
    assert artifacts[0].content_hash == result.as_evidence()["evidence_hash"]


async def test_full_task_lifecycle_through_the_ledger(
    live_client: TerminusClient, scratch_database: str, pack
):
    """Section 9.2/9.3/9.5/9.8 against the real graph."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    service = TaskService(writer)
    system = Actor(alias="system", role="control-plane", kind=ActorKind.SYSTEM)
    worker = Actor(alias="implementer-i12", role="implementer", kind=ActorKind.WORKER)
    gate = Actor(alias="wiring-w05", role="integration_verifier", kind=ActorKind.GATE)

    creation = await service.create_task(
        entity_id="T-010",
        project_document_id="Project/EFAH-001",
        title="TerminusDB adapter",
        objective="async adapter against the real API",
        actor=system,
        allowed_paths=["src/integrations/terminusdb.py"],
    )
    task_id = creation.task.document_id

    work_unit = WorkUnit(
        entity_id="WU-0010",
        envelope=Envelope(schema_id="efah.work_unit", created_by_alias="system"),
        task=task_id,
        objective="build the adapter",
        contract_version="1.1",
        success_conditions={"type": "command_exit", "command": "pytest", "expected_exit": 0},
        failure_conditions=["missing_wiring"],
    )
    await writer.write([work_unit], message="work unit for T-010")

    await service.record(task_id, TaskEventType.TaskReady, system)
    _, lease = await service.acquire_lease(
        task_document_id=task_id,
        work_unit_document_id=work_unit.document_id,
        alias="implementer-i12",
        role="implementer",
        repository="efah-harness",
        branch="feat/ws-b-terminusdb",
        ttl_seconds=3600,
    )
    assert lease.generation == 1

    await service.record(task_id, TaskEventType.WorkerStarted, worker, lease=lease)
    await service.record(
        task_id, TaskEventType.ToolCallRecorded, worker, payload={"tool": "bash", "exit": 0}, lease=lease
    )
    _, projection = await service.submit_candidate(
        task_id, worker, lease=lease, artifact_ids=["A-1"]
    )
    assert projection.state is TaskState.CANDIDATE_COMPLETE

    # Section 9.3: the worker cannot produce PASSED, by any route.
    with pytest.raises(LedgerAuthorityViolation):
        await service.record(task_id, TaskEventType.GatePassed, worker, lease=lease)
    with pytest.raises(LedgerAuthorityViolation):
        await service.record(
            task_id, TaskEventType.TaskClosed, worker, lease=lease
        )

    await service.record(task_id, TaskEventType.EvaluationStarted, gate)
    _, projection = await service.record(task_id, TaskEventType.GatePassed, gate)
    assert projection.state is TaskState.PASSED

    # The ledger is append-only and the projection is a fold of it.
    assert await service.ledger.verify_append_only(task_id)
    events = await service.ledger.events(task_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert service.ledger.fold(task_id, events).state is TaskState.PASSED

    # Section 9.8: durations come from the recorded system events.
    assert projection.queued_at is not None
    assert projection.claimed_at is not None
    assert projection.started_at is not None
    assert projection.candidate_submitted_at is not None
    assert projection.verification_started_at is not None
    assert projection.queue_seconds is not None and projection.queue_seconds >= 0
    assert projection.total_wall_clock_seconds > 0

    stored_task = await writer.read(Task, "T-010")
    assert isinstance(stored_task, Task)
    assert stored_task.state is TaskState.PASSED


async def test_expired_lease_submission_is_rejected_as_stale(
    live_client: TerminusClient, scratch_database: str, pack
):
    """Section 9.5 fencing."""
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    service = TaskService(writer)
    system = Actor(alias="system", role="control-plane", kind=ActorKind.SYSTEM)
    worker = Actor(alias="implementer-i12", role="implementer", kind=ActorKind.WORKER)

    creation = await service.create_task(
        entity_id="T-011",
        project_document_id="Project/EFAH-001",
        title="stale lease",
        objective="prove fencing",
        actor=system,
    )
    work_unit = WorkUnit(
        entity_id="WU-0011",
        envelope=Envelope(schema_id="efah.work_unit", created_by_alias="system"),
        task=creation.task.document_id,
        objective="x",
        contract_version="1.1",
    )
    await writer.write([work_unit], message="work unit for T-011")
    _, lease = await service.acquire_lease(
        task_document_id=creation.task.document_id,
        work_unit_document_id=work_unit.document_id,
        alias="implementer-i12",
        role="implementer",
        repository="efah-harness",
        branch="feat/ws-b-terminusdb",
        ttl_seconds=3600,
    )

    expired = lease.model_copy(
        update={"expires_at": lease.acquired_at - timedelta(seconds=1)}
    )
    with pytest.raises(StaleLeaseRejected):
        await service.submit_candidate(
            creation.task.document_id, worker, lease=expired, artifact_ids=["A-2"]
        )

    superseded = lease.model_copy(update={"state": LeaseState.superseded})
    with pytest.raises(StaleLeaseRejected):
        await service.submit_candidate(
            creation.task.document_id, worker, lease=superseded, artifact_ids=["A-2"]
        )

    # A second acquisition fences the first: the generation advances.
    _, newer = await service.acquire_lease(
        task_document_id=creation.task.document_id,
        work_unit_document_id=work_unit.document_id,
        alias="implementer-i12",
        role="implementer",
        repository="efah-harness",
        branch="feat/ws-b-terminusdb",
        ttl_seconds=3600,
    )
    assert newer.generation == lease.generation + 1


async def test_project_state_transitions_respect_section_6_2(
    live_client: TerminusClient, scratch_database: str, pack
):
    result = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    writer = ProvenanceWriter(
        live_client, database=scratch_database, branch=result.branch, author_alias=AUTHOR
    )
    repo = ProjectRepository(writer)

    summary = await repo.summary("EFAH-001")
    assert summary.contract_id == CONTRACT_ID
    assert summary.entity_counts["Project"] == 1
    assert summary.dependency_edges

    project, receipt = await repo.set_state(
        "EFAH-001", ProjectState.VERIFIED_COMPLETE, reason="integration test"
    )
    assert project.state is ProjectState.VERIFIED_COMPLETE
    assert receipt.commit_id

    with pytest.raises(TerminalStateViolation):
        await repo.set_state("EFAH-001", ProjectState.RUNNING, reason="should not be allowed")


async def test_reimporting_the_same_pack_creates_a_second_isolated_branch(
    live_client: TerminusClient, scratch_database: str, pack
):
    first = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database
    )
    second = await import_project_pack(
        live_client, pack, author_alias=AUTHOR, database=scratch_database, branch="import-second"
    )
    assert first.branch != second.branch
    assert second.main_head_unchanged
    assert first.branch in second.branches_before
