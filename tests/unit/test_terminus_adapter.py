"""Unit tests for the TerminusDB adapter.

These use ``httpx.MockTransport`` replaying the *measured* response bodies from
the live 12.0.6 server, so they fail if the adapter stops parsing what the server
actually sends -- not merely if it stops matching an invented shape.
"""

from __future__ import annotations

import json

import httpx
import pytest

from governance.states import FailureClass
from integrations.terminusdb import (
    BranchRef,
    CommitRecord,
    TerminusAlreadyExists,
    TerminusAuthError,
    TerminusClient,
    TerminusConfig,
    TerminusNotFound,
    TerminusSchemaCheckFailure,
    _decode_documents,
)

# Verbatim from `GET /api/document/admin/probe/local/_commits?type=Branch`.
BRANCH_NDJSON = (
    '{"@id":"Branch/main","@type":"Branch","name":"main",'
    '"head":"InitialCommit/hse8zees7dwezv2suzt6u3mcy4btaqh"}\n'
    '{"@id":"Branch/probe1","@type":"Branch","name":"probe1",'
    '"head":"ValidCommit/yjxhrmpm0iyw88aewdj434n0uqlxn3s"}\n'
)

LOG_JSON = [
    {
        "@id": "ValidCommit/yjxhrmpm0iyw88aewdj434n0uqlxn3s",
        "@type": "ValidCommit",
        "author": "efah",
        "identifier": "yjxhrmpm0iyw88aewdj434n0uqlxn3s",
        "message": "probe doc",
        "parent": "ValidCommit/5ikdc9ve1hd8d3iq76v1zu4roskhgz6",
        "timestamp": 1785648185.80388999,
        "user": "terminusdb://system/data/User/admin",
    }
]

ALREADY_EXISTS = {
    "@type": "api:DbCreateErrorResponse",
    "api:error": {"@type": "api:DatabaseAlreadyExists", "api:database_name": "efah"},
    "api:message": "Database already exists.",
    "api:status": "api:failure",
}

SCHEMA_FAILURE = {
    "@type": "api:InsertDocumentErrorResponse",
    "api:error": {
        "@type": "api:SchemaCheckFailure",
        "api:witnesses": [{"@type": "references_untyped_object"}],
    },
    "api:status": "api:failure",
}

AUTH_FAILURE = {
    "@type": "api:ErrorResponse",
    "api:error": {"@type": "api:IncorrectAuthenticationError"},
    "api:message": "Incorrect authentication information",
    "api:status": "api:failure",
}


def make_client(handler) -> TerminusClient:
    transport = httpx.MockTransport(handler)
    config = TerminusConfig(endpoint="http://terminus.test", password="secret")
    http = httpx.AsyncClient(base_url=config.base_url, transport=transport, auth=("admin", "secret"))
    return TerminusClient(config, client=http)


def test_config_refuses_empty_password():
    with pytest.raises(ValueError):
        TerminusConfig(password="")


def test_decode_documents_handles_ndjson_and_array():
    assert len(_decode_documents(BRANCH_NDJSON)) == 2
    assert _decode_documents('[{"a":1}]') == [{"a": 1}]
    assert _decode_documents("  \n ") == []


def test_branch_ref_strips_commit_prefix():
    assert BranchRef(name="x", head="ValidCommit/abc").head_commit_id == "abc"
    assert BranchRef(name="x", head=None).head_commit_id is None


def test_commit_record_immutability_signal():
    record = CommitRecord.from_json(LOG_JSON[0])
    assert record.is_immutable
    assert record.author == "efah"
    assert CommitRecord(identifier="", commit_type="ValidCommit", author="a", message="m", timestamp=0).is_immutable is False


async def test_list_branches_parses_ndjson_and_sorts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/document/admin/efah/local/_commits"
        assert request.url.params["type"] == "Branch"
        return httpx.Response(200, text=BRANCH_NDJSON)

    async with make_client(handler) as client:
        branches = await client.list_branches("efah")
    assert [b.name for b in branches] == ["main", "probe1"]
    assert branches[1].head_commit_id == "yjxhrmpm0iyw88aewdj434n0uqlxn3s"


async def test_head_commit_raises_for_unknown_branch():
    async with make_client(lambda r: httpx.Response(200, text=BRANCH_NDJSON)) as client:
        assert await client.head_commit("efah", "probe1") == "yjxhrmpm0iyw88aewdj434n0uqlxn3s"
        with pytest.raises(TerminusNotFound):
            await client.head_commit("efah", "no-such-branch")


async def test_database_exists_uses_head_and_status():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200 if request.url.path.endswith("efah") else 404)

    async with make_client(handler) as client:
        assert await client.database_exists("efah") is True
        assert await client.database_exists("nope") is False
    assert calls == ["HEAD", "HEAD"]


async def test_ensure_database_is_idempotent_against_already_exists():
    """A concurrent creator must not turn into a spurious infrastructure failure."""
    state = {"head": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(state["head"])
        return httpx.Response(400, json=ALREADY_EXISTS)

    async with make_client(handler) as client:
        assert await client.ensure_database("efah", label="l", comment="c") is False


async def test_create_database_error_is_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=ALREADY_EXISTS)

    async with make_client(handler) as client:
        with pytest.raises(TerminusAlreadyExists) as exc:
            await client.create_database("efah", label="l", comment="c")
    assert exc.value.api_error_type == "api:DatabaseAlreadyExists"


async def test_schema_check_failure_is_a_wiring_failure_not_infrastructure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=SCHEMA_FAILURE)

    async with make_client(handler) as client:
        with pytest.raises(TerminusSchemaCheckFailure) as exc:
            await client.insert_documents("efah", [{"@type": "X"}], author="a", message="m")
    assert exc.value.failure_class is FailureClass.WIRING_FAILURE


async def test_401_maps_to_protected_access():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=AUTH_FAILURE)

    async with make_client(handler) as client:
        with pytest.raises(TerminusAuthError) as exc:
            await client.info()
    assert exc.value.failure_class is FailureClass.PROTECTED_ACCESS
    assert exc.value.status == 401


@pytest.mark.parametrize(("author", "message"), [("", "m"), ("a", ""), ("   ", "m")])
async def test_write_without_attribution_is_refused(author, message):
    """Contract Section 15.2: a material write is attributable or it does not happen."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("adapter reached the network without attribution")

    async with make_client(handler) as client:
        with pytest.raises(ValueError):
            await client.insert_documents("efah", [{"@type": "X"}], author=author, message=message)


async def test_insert_documents_sends_author_message_and_graph_type():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text='["terminusdb:///data/Widget/W-1"]')

    async with make_client(handler) as client:
        ids = await client.insert_documents(
            "efah",
            [{"@type": "Widget", "widget_id": "W-1"}],
            author="ws-b",
            message="probe",
            branch="import-1",
            graph_type="schema",
        )
    assert ids == ["terminusdb:///data/Widget/W-1"]
    assert seen["params"] == {"graph_type": "schema", "author": "ws-b", "message": "probe"}
    assert seen["body"] == [{"@type": "Widget", "widget_id": "W-1"}]


async def test_empty_write_makes_no_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("empty write should not reach the network")

    async with make_client(handler) as client:
        assert await client.insert_documents("efah", [], author="a", message="m") == []


async def test_log_parses_commit_records():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/log/admin/efah/local/branch/main"
        return httpx.Response(200, json=LOG_JSON)

    async with make_client(handler) as client:
        entries = await client.log("efah")
        latest = await client.latest_commit("efah")
    assert entries[0].identifier == "yjxhrmpm0iyw88aewdj434n0uqlxn3s"
    assert latest is not None and latest.author == "efah"


async def test_query_bindings_unwraps_woql_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/woql/admin/efah/local/branch/main"
        return httpx.Response(
            200,
            json={
                "@type": "api:WoqlResponse",
                "api:status": "api:success",
                "bindings": [{"S": "Widget/W-1"}],
            },
        )

    async with make_client(handler) as client:
        assert await client.query_bindings("efah", {"@type": "Triple"}) == [{"S": "Widget/W-1"}]


async def test_transport_error_is_wrapped_as_terminus_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with make_client(handler) as client:
        with pytest.raises(Exception) as exc:
            await client.info()
    assert "failed to reach TerminusDB" in str(exc.value)


def test_branch_and_commit_paths():
    client = TerminusClient(TerminusConfig(password="x"))
    assert client.branch_path("efah", "b1") == "admin/efah/local/branch/b1"
    assert client.commits_path("efah") == "admin/efah/local/_commits"
