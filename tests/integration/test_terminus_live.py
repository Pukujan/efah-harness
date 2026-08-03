"""The adapter against the real TerminusDB at localhost:6363.

Every assertion here corresponds to something the adapter's docstring claims
about the measured API. If TerminusDB changes one of those shapes, this fails
rather than the harness silently writing nowhere.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from governance.envelope import Envelope
from governance.states import ProjectState
from integrations.terminusdb import (
    MAIN_BRANCH,
    TerminusAlreadyExists,
    TerminusClient,
    TerminusConfig,
    TerminusNotFound,
    TerminusSchemaCheckFailure,
)
from ontology import terminus_schema_documents, to_terminus_document
from ontology.schema import Dependency, DependencyEdgeType, DependencyKind, Project

AUTHOR = "ws-b-terminusdb"
MAIN_ENDPOINT = "http://localhost:6363"


def require_env(name: str) -> str:
    """Skip only when the credential is absent.

    Never skip because the server is unreachable: a refused connection is a real
    failure of a required service (``FAILED_INFRASTRUCTURE``), not a reason to
    report green.
    """
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
    """A throwaway database, dropped afterwards even if the test fails."""
    name = f"efah_test_{uuid.uuid4().hex[:10]}"
    await live_client.ensure_database(name, label="EFAH test", comment="integration test scratch")
    try:
        yield name
    finally:
        # Cleanup must not mask the failure that brought us here.
        with contextlib.suppress(Exception):
            await live_client.delete_database(name)


async def test_server_is_reachable_and_reports_a_version(live_client: TerminusClient):
    version = await live_client.server_version()
    assert version, "TerminusDB reported no version"
    assert version.split(".")[0].isdigit()


async def test_fresh_database_has_exactly_main(live_client: TerminusClient, scratch_database: str):
    assert await live_client.branch_names(scratch_database) == [MAIN_BRANCH]
    head = await live_client.head_commit(scratch_database, MAIN_BRANCH)
    assert head, "a fresh database must still have an initial commit"


async def test_ensure_database_is_idempotent(live_client: TerminusClient, scratch_database: str):
    assert await live_client.database_exists(scratch_database) is True
    assert await live_client.ensure_database(scratch_database, label="l", comment="c") is False
    with pytest.raises(TerminusAlreadyExists):
        await live_client.create_database(scratch_database, label="l", comment="c")


async def test_branch_creation_leaves_main_untouched(
    live_client: TerminusClient, scratch_database: str
):
    """The property GATE-D1-01 A1 is built on."""
    main_before = await live_client.head_commit(scratch_database, MAIN_BRANCH)
    assert await live_client.ensure_branch(scratch_database, "iso-1") is True
    assert await live_client.ensure_branch(scratch_database, "iso-1") is False

    await live_client.insert_documents(
        scratch_database,
        terminus_schema_documents(),
        author=AUTHOR,
        message="ontology",
        branch="iso-1",
        graph_type="schema",
    )
    assert sorted(await live_client.branch_names(scratch_database)) == ["iso-1", "main"]
    assert await live_client.head_commit(scratch_database, MAIN_BRANCH) == main_before
    assert await live_client.head_commit(scratch_database, "iso-1") != main_before


async def test_generated_ontology_installs_on_the_live_server(
    live_client: TerminusClient, scratch_database: str
):
    documents = terminus_schema_documents()
    created = await live_client.insert_documents(
        scratch_database,
        documents,
        author=AUTHOR,
        message="control-plane ontology",
        graph_type="schema",
    )
    assert len(created) == len(documents)
    stored = await live_client.get_documents(scratch_database, graph_type="schema")
    stored_ids = {d.get("@id") for d in stored}
    assert {"ControlPlaneEntity", "Project", "Dependency", "TaskEvent"} <= stored_ids


async def test_write_read_and_commit_id_round_trip(
    live_client: TerminusClient, scratch_database: str
):
    await live_client.ensure_branch(scratch_database, "rt")
    await live_client.insert_documents(
        scratch_database,
        terminus_schema_documents(),
        author=AUTHOR,
        message="ontology",
        branch="rt",
        graph_type="schema",
    )
    project = Project(
        entity_id="EFAH-RT",
        envelope=Envelope(schema_id="efah.project", created_by_alias=AUTHOR),
        name="round trip",
        mode="autonomous",
        state=ProjectState.RUNNING,
        pack_manifest_hash="sha256:deadbeef",
    )
    ids = await live_client.insert_documents(
        scratch_database,
        [to_terminus_document(project)],
        author=AUTHOR,
        message="round trip project",
        branch="rt",
    )
    assert ids == ["terminusdb:///data/Project/EFAH-RT"]

    commit_id = await live_client.head_commit(scratch_database, "rt")
    latest = await live_client.latest_commit(scratch_database, branch="rt")
    assert latest is not None
    assert latest.identifier == commit_id
    assert latest.author == AUTHOR
    assert latest.message == "round trip project"
    assert latest.is_immutable

    stored = await live_client.get_document(scratch_database, "Project/EFAH-RT", branch="rt")
    assert stored is not None
    assert stored["pack_manifest_hash"] == "sha256:deadbeef"
    assert stored["envelope"]["created_by_alias"] == AUTHOR


async def test_referential_integrity_is_enforced_by_the_graph(
    live_client: TerminusClient, scratch_database: str
):
    """A dangling dependency edge must be rejected, not stored."""
    await live_client.insert_documents(
        scratch_database,
        terminus_schema_documents(),
        author=AUTHOR,
        message="ontology",
        graph_type="schema",
    )
    edge = Dependency(
        entity_id="EDGE-dangling",
        envelope=Envelope(schema_id="efah.dependency", created_by_alias=AUTHOR),
        edge_type=DependencyEdgeType.depends_on,
        kind=DependencyKind.task,
        source="Project/does-not-exist",
        target="Project/also-missing",
    )
    with pytest.raises(TerminusSchemaCheckFailure):
        await live_client.insert_documents(
            scratch_database, [to_terminus_document(edge)], author=AUTHOR, message="dangling"
        )


async def test_log_is_append_only_and_parents_chain(
    live_client: TerminusClient, scratch_database: str
):
    await live_client.insert_documents(
        scratch_database,
        [{"@type": "Class", "@id": "Note", "@key": {"@type": "Lexical", "@fields": ["k"]}, "k": "xsd:string"}],
        author=AUTHOR,
        message="note schema",
        graph_type="schema",
    )
    for index in range(3):
        await live_client.insert_documents(
            scratch_database,
            [{"@type": "Note", "k": f"n{index}"}],
            author=AUTHOR,
            message=f"note {index}",
        )
    entries = await live_client.log(scratch_database)
    messages = [e.message for e in entries]
    assert messages[:3] == ["note 2", "note 1", "note 0"]
    assert entries[0].parent is not None
    assert all(e.author in (AUTHOR, "system") for e in entries)


async def test_woql_query_returns_bindings(live_client: TerminusClient, scratch_database: str):
    await live_client.insert_documents(
        scratch_database,
        [{"@type": "Class", "@id": "Note", "@key": {"@type": "Lexical", "@fields": ["k"]}, "k": "xsd:string"}],
        author=AUTHOR,
        message="note schema",
        graph_type="schema",
    )
    await live_client.insert_documents(
        scratch_database, [{"@type": "Note", "k": "hello"}], author=AUTHOR, message="one note"
    )
    bindings = await live_client.query_bindings(
        scratch_database,
        {
            "@type": "Triple",
            "subject": {"@type": "NodeValue", "variable": "S"},
            "predicate": {"@type": "NodeValue", "node": "@schema:k"},
            "object": {"@type": "Value", "variable": "O"},
        },
    )
    values = [b["O"]["@value"] for b in bindings if isinstance(b.get("O"), dict)]
    assert "hello" in values


async def test_unknown_branch_raises_not_found(live_client: TerminusClient, scratch_database: str):
    with pytest.raises(TerminusNotFound):
        await live_client.head_commit(scratch_database, "no-such-branch")


async def test_delete_branch_removes_it(live_client: TerminusClient, scratch_database: str):
    await live_client.create_branch(scratch_database, "temp")
    assert "temp" in await live_client.branch_names(scratch_database)
    await live_client.delete_branch(scratch_database, "temp")
    assert "temp" not in await live_client.branch_names(scratch_database)
