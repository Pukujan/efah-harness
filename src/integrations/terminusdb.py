"""Async adapter for TerminusDB, the authoritative graph (contract Section 15.2).

Every endpoint and payload shape in this module was measured against the live
server at ``http://localhost:6363`` (TerminusDB ``12.0.6``, storage ``2``,
``terminusdb_store 0.19.8``) rather than guessed. Contract Section 7.1 ranks a
safe probe above external documentation, and that ordering mattered here: the
vendor's own API index (Context7 snapshot
``C7-terminusdb-main-bcc5b287``) advertises ``GET /api/branch/:path`` for listing
branches, and the running server answers that route with HTTP 405. The measured
route -- reading ``Branch`` documents out of the ``_commits`` graph -- is what
this adapter uses. See ``docs/decisions/DEC-101``.

Measured API surface (all under basic auth):

===========================================================  ===============================
Operation                                                    Route
===========================================================  ===============================
server info                                                  ``GET  /api/info``
list databases                                               ``GET  /api/db``
database exists                                              ``HEAD /api/db/{org}/{db}``
create database                                              ``POST /api/db/{org}/{db}``
delete database                                              ``DELETE /api/db/{org}/{db}``
list branches                                                ``GET  /api/document/{org}/{db}/local/_commits?type=Branch``
create branch                                                ``POST /api/branch/{org}/{db}/local/branch/{name}``
delete branch                                                ``DELETE /api/branch/{org}/{db}/local/branch/{name}``
insert documents                                             ``POST /api/document/{path}?graph_type=&author=&message=``
replace documents                                            ``PUT  /api/document/{path}?graph_type=&author=&message=``
read documents                                               ``GET  /api/document/{path}?graph_type=&type=&as_list=true``
commit log                                                   ``GET  /api/log/{org}/{db}/local/branch/{name}``
WOQL                                                         ``POST /api/woql/{path}``
===========================================================  ===============================

Document reads and branch listings return newline-delimited JSON unless
``as_list=true`` is supplied; :func:`_decode_documents` handles both.

No vendor SDK is imported: the transport is ``httpx``, which the contract's
selected stack already pins. ``terminusdb-client`` would be a second HTTP client
with a synchronous core, so integrating it here would *lose* the async property
the control plane needs -- this is an adapter over the documented HTTP API, not
a reimplementation of the database (Section 14.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

import httpx

from governance.states import FailureClass

#: Contract Section 15.2: a material write must be attributable. The adapter
#: refuses to write without an author alias and a message, so an anonymous
#: commit cannot reach the graph by omission.
_MISSING_ATTRIBUTION = "a material write needs both author alias and message (contract Section 15.2)"

DEFAULT_ENDPOINT = "http://localhost:6363"
DEFAULT_ORG = "admin"
DEFAULT_USER = "admin"
MAIN_BRANCH = "main"


class TerminusError(RuntimeError):
    """A typed failure from the authoritative graph.

    Carries the HTTP status and TerminusDB's own ``api:error`` discriminator so
    callers classify before retrying (contract Section 10.6) instead of
    string-matching a message.
    """

    failure_class: FailureClass = FailureClass.INFRASTRUCTURE_FAILURE

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None) -> None:
        self.status = status
        self.payload = payload
        super().__init__(message)

    @property
    def api_error_type(self) -> str | None:
        if isinstance(self.payload, Mapping):
            err = self.payload.get("api:error")
            if isinstance(err, Mapping):
                return err.get("@type")
        return None


class TerminusAuthError(TerminusError):
    """401/403. Used by the protected-identity isolation proof (GATE-D1-08)."""

    failure_class = FailureClass.PROTECTED_ACCESS


class TerminusNotFound(TerminusError):
    failure_class = FailureClass.INFRASTRUCTURE_FAILURE


class TerminusAlreadyExists(TerminusError):
    """Database or branch already exists. Distinguished so `ensure_*` is idempotent."""

    failure_class = FailureClass.INFRASTRUCTURE_FAILURE


class TerminusSchemaCheckFailure(TerminusError):
    """The graph rejected a document. Not a transport problem -- a data problem."""

    failure_class = FailureClass.WIRING_FAILURE


@dataclass(frozen=True)
class TerminusConfig:
    """Connection settings for one TerminusDB instance."""

    endpoint: str = DEFAULT_ENDPOINT
    user: str = DEFAULT_USER
    password: str = ""
    org: str = DEFAULT_ORG
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.password:
            raise ValueError("TerminusConfig requires a password; resolve it through SecretResolver")

    @property
    def base_url(self) -> str:
        return self.endpoint.rstrip("/")


@dataclass(frozen=True)
class BranchRef:
    """A branch as the ``_commits`` graph reports it."""

    name: str
    head: str | None

    @property
    def head_commit_id(self) -> str | None:
        """``ValidCommit/abc123`` -> ``abc123``."""
        if not self.head:
            return None
        return self.head.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class CommitRecord:
    """One entry of ``GET /api/log/...``."""

    identifier: str
    commit_type: str
    author: str
    message: str
    timestamp: float
    parent: str | None = None
    user: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_immutable(self) -> bool:
        """TerminusDB commits are content-addressed layers; a commit id that
        resolves is by construction not rewritable in place. GATE-D1-01 A3 asks
        for evidence of that, which is the presence of a layer-backed identifier
        plus a non-system author."""
        return bool(self.identifier) and self.commit_type in {"ValidCommit", "InitialCommit"}

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "CommitRecord":
        return cls(
            identifier=doc.get("identifier", ""),
            commit_type=doc.get("@type", ""),
            author=doc.get("author", ""),
            message=doc.get("message", ""),
            timestamp=float(doc.get("timestamp", 0.0)),
            parent=doc.get("parent"),
            user=doc.get("user"),
            raw=dict(doc),
        )


def _decode_documents(text: str) -> list[Any]:
    """Parse TerminusDB's newline-delimited JSON *or* a JSON array."""
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] == "[":
        parsed = json.loads(stripped)
        return list(parsed) if isinstance(parsed, list) else [parsed]
    docs: list[Any] = []
    for line in stripped.splitlines():
        line = line.strip()
        if line:
            docs.append(json.loads(line))
    return docs


def _quote(value: str) -> str:
    return quote(value, safe="")


class TerminusClient:
    """Async client for one TerminusDB instance.

    Usage::

        async with TerminusClient(config) as db:
            await db.ensure_database("efah", label="EFAH control plane")
            await db.create_branch("efah", "import/pack-abc")
            await db.insert_documents("efah", docs, branch="import/pack-abc",
                                      author="ws-b", message="import project pack")
    """

    def __init__(self, config: TerminusConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            auth=(config.user, config.password),
            timeout=config.timeout,
        )

    @property
    def config(self) -> TerminusConfig:
        return self._config

    @property
    def endpoint(self) -> str:
        return self._config.base_url

    async def __aenter__(self) -> "TerminusClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- paths -------------------------------------------------------------

    def branch_path(self, database: str, branch: str = MAIN_BRANCH) -> str:
        return f"{self._config.org}/{database}/local/branch/{branch}"

    def commits_path(self, database: str) -> str:
        return f"{self._config.org}/{database}/local/_commits"

    # -- transport ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        expect_json: bool = True,
    ) -> Any:
        try:
            response = await self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:  # transport, DNS, timeout
            raise TerminusError(f"{method} {path} failed to reach TerminusDB: {exc}") from exc

        if response.status_code >= 400:
            raise self._error_for(response, method, path)
        if not expect_json:
            return response
        return _decode_documents(response.text)

    def _error_for(self, response: httpx.Response, method: str, path: str) -> TerminusError:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        api_type = None
        if isinstance(payload, Mapping):
            err = payload.get("api:error")
            if isinstance(err, Mapping):
                api_type = err.get("@type")
        message = f"{method} {path} -> HTTP {response.status_code}"
        if api_type:
            message = f"{message} ({api_type})"

        if response.status_code in (401, 403):
            return TerminusAuthError(message, status=response.status_code, payload=payload)
        if api_type in {"api:DatabaseAlreadyExists", "api:BranchExistsError"}:
            return TerminusAlreadyExists(message, status=response.status_code, payload=payload)
        if api_type == "api:SchemaCheckFailure":
            return TerminusSchemaCheckFailure(message, status=response.status_code, payload=payload)
        if response.status_code == 404:
            return TerminusNotFound(message, status=response.status_code, payload=payload)
        return TerminusError(message, status=response.status_code, payload=payload)

    # -- server ------------------------------------------------------------

    async def info(self) -> dict[str, Any]:
        """``GET /api/info``. Also the cheapest authentication probe."""
        result = await self._request("GET", "/api/info")
        return result[0] if isinstance(result, list) and result else {}

    async def server_version(self) -> str:
        info = await self.info()
        return str(info.get("api:info", {}).get("terminusdb", {}).get("version", ""))

    # -- databases ---------------------------------------------------------

    async def list_databases(self) -> list[str]:
        docs = await self._request("GET", "/api/db")
        return [d["path"] for d in docs if isinstance(d, Mapping) and "path" in d]

    async def database_exists(self, database: str) -> bool:
        """``HEAD /api/db/{org}/{db}`` -> 200 present, 404 absent (measured)."""
        path = f"/api/db/{_quote(self._config.org)}/{_quote(database)}"
        try:
            response = await self._client.request("HEAD", path)
        except httpx.HTTPError as exc:
            raise TerminusError(f"HEAD {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise TerminusAuthError(f"HEAD {path} -> HTTP {response.status_code}", status=response.status_code)
        return response.status_code == 200

    async def create_database(
        self,
        database: str,
        *,
        label: str,
        comment: str,
        schema: bool = True,
        public: bool = False,
        prefixes: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"label": label, "comment": comment, "schema": schema, "public": public}
        if prefixes:
            body["prefixes"] = dict(prefixes)
        result = await self._request(
            "POST", f"/api/db/{_quote(self._config.org)}/{_quote(database)}", json_body=body
        )
        return result[0] if isinstance(result, list) and result else {}

    async def ensure_database(
        self,
        database: str,
        *,
        label: str,
        comment: str,
        schema: bool = True,
    ) -> bool:
        """Create *database* if absent. Returns True when it was created now.

        Idempotent against the measured ``api:DatabaseAlreadyExists`` response,
        so a concurrent creator does not turn into a spurious failure.
        """
        if await self.database_exists(database):
            return False
        try:
            await self.create_database(database, label=label, comment=comment, schema=schema)
        except TerminusAlreadyExists:
            return False
        return True

    async def delete_database(self, database: str) -> None:
        await self._request("DELETE", f"/api/db/{_quote(self._config.org)}/{_quote(database)}")

    # -- branches ----------------------------------------------------------

    async def list_branches(self, database: str) -> list[BranchRef]:
        """Measured route. ``GET /api/branch/{path}`` is 405 on 12.0.6."""
        docs = await self._request(
            "GET",
            f"/api/document/{self.commits_path(database)}",
            params={"graph_type": "instance", "type": "Branch", "as_list": "true"},
        )
        branches = [
            BranchRef(name=d["name"], head=d.get("head"))
            for d in docs
            if isinstance(d, Mapping) and d.get("@type") == "Branch"
        ]
        return sorted(branches, key=lambda b: b.name)

    async def branch_names(self, database: str) -> list[str]:
        return [b.name for b in await self.list_branches(database)]

    async def create_branch(
        self, database: str, branch: str, *, origin: str | None = MAIN_BRANCH
    ) -> None:
        """Branch *branch* off *origin* (``None`` for an empty branch)."""
        body: dict[str, Any] = {}
        if origin is not None:
            body["origin"] = f"{self._config.org}/{database}/local/branch/{origin}"
        await self._request(
            "POST",
            f"/api/branch/{self.branch_path(database, branch)}",
            json_body=body,
        )

    async def ensure_branch(self, database: str, branch: str, *, origin: str = MAIN_BRANCH) -> bool:
        """Returns True when the branch was created by this call."""
        if branch in await self.branch_names(database):
            return False
        try:
            await self.create_branch(database, branch, origin=origin)
        except TerminusAlreadyExists:
            return False
        return True

    async def delete_branch(self, database: str, branch: str) -> None:
        await self._request("DELETE", f"/api/branch/{self.branch_path(database, branch)}")

    async def head_commit(self, database: str, branch: str = MAIN_BRANCH) -> str | None:
        for ref in await self.list_branches(database):
            if ref.name == branch:
                return ref.head_commit_id
        raise TerminusNotFound(f"branch {branch!r} not present in {database!r}")

    # -- documents ---------------------------------------------------------

    async def insert_documents(
        self,
        database: str,
        documents: Sequence[Mapping[str, Any]],
        *,
        author: str,
        message: str,
        branch: str = MAIN_BRANCH,
        graph_type: str = "instance",
        full_replace: bool = False,
    ) -> list[str]:
        """POST documents; returns the ids TerminusDB assigned.

        *author* and *message* are mandatory -- see :data:`_MISSING_ATTRIBUTION`.
        """
        return await self._write_documents(
            "POST",
            database,
            documents,
            author=author,
            message=message,
            branch=branch,
            graph_type=graph_type,
            extra_params={"full_replace": "true"} if full_replace else None,
        )

    async def replace_documents(
        self,
        database: str,
        documents: Sequence[Mapping[str, Any]],
        *,
        author: str,
        message: str,
        branch: str = MAIN_BRANCH,
        graph_type: str = "instance",
        create: bool = False,
    ) -> list[str]:
        return await self._write_documents(
            "PUT",
            database,
            documents,
            author=author,
            message=message,
            branch=branch,
            graph_type=graph_type,
            extra_params={"create": "true"} if create else None,
        )

    async def _write_documents(
        self,
        method: str,
        database: str,
        documents: Sequence[Mapping[str, Any]],
        *,
        author: str,
        message: str,
        branch: str,
        graph_type: str,
        extra_params: Mapping[str, str] | None = None,
    ) -> list[str]:
        if not author.strip() or not message.strip():
            raise ValueError(_MISSING_ATTRIBUTION)
        if not documents:
            return []
        params: dict[str, Any] = {"graph_type": graph_type, "author": author, "message": message}
        if extra_params:
            params.update(extra_params)
        result = await self._request(
            method,
            f"/api/document/{self.branch_path(database, branch)}",
            params=params,
            json_body=list(documents),
        )
        ids: list[str] = []
        for entry in result:
            if isinstance(entry, str):
                ids.append(entry)
            elif isinstance(entry, list):
                ids.extend(str(x) for x in entry)
        return ids

    async def delete_documents(
        self,
        database: str,
        ids: Iterable[str],
        *,
        author: str,
        message: str,
        branch: str = MAIN_BRANCH,
        graph_type: str = "instance",
    ) -> None:
        if not author.strip() or not message.strip():
            raise ValueError(_MISSING_ATTRIBUTION)
        id_list = list(ids)
        if not id_list:
            return
        await self._request(
            "DELETE",
            f"/api/document/{self.branch_path(database, branch)}",
            params={"graph_type": graph_type, "author": author, "message": message},
            json_body=id_list,
        )

    async def get_documents(
        self,
        database: str,
        *,
        branch: str = MAIN_BRANCH,
        doc_type: str | None = None,
        graph_type: str = "instance",
        skip: int | None = None,
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"graph_type": graph_type, "as_list": "true"}
        if doc_type:
            params["type"] = doc_type
        if skip is not None:
            params["skip"] = skip
        if count is not None:
            params["count"] = count
        docs = await self._request(
            "GET", f"/api/document/{self.branch_path(database, branch)}", params=params
        )
        return [d for d in docs if isinstance(d, dict)]

    async def get_document(
        self,
        database: str,
        document_id: str,
        *,
        branch: str = MAIN_BRANCH,
        graph_type: str = "instance",
    ) -> dict[str, Any] | None:
        try:
            docs = await self._request(
                "GET",
                f"/api/document/{self.branch_path(database, branch)}",
                params={"graph_type": graph_type, "id": document_id},
            )
        except TerminusNotFound:
            return None
        for doc in docs:
            if isinstance(doc, dict):
                return doc
        return None

    # -- history and query -------------------------------------------------

    async def log(
        self, database: str, *, branch: str = MAIN_BRANCH, count: int | None = None, start: int | None = None
    ) -> list[CommitRecord]:
        params: dict[str, Any] = {}
        if count is not None:
            params["count"] = count
        if start is not None:
            params["start"] = start
        docs = await self._request(
            "GET", f"/api/log/{self.branch_path(database, branch)}", params=params or None
        )
        return [CommitRecord.from_json(d) for d in docs if isinstance(d, Mapping)]

    async def latest_commit(self, database: str, *, branch: str = MAIN_BRANCH) -> CommitRecord | None:
        entries = await self.log(database, branch=branch, count=1)
        return entries[0] if entries else None

    async def query(
        self, database: str, woql: Mapping[str, Any], *, branch: str = MAIN_BRANCH
    ) -> dict[str, Any]:
        """POST a WOQL query document. Returns the raw ``api:WoqlResponse``."""
        result = await self._request(
            "POST", f"/api/woql/{self.branch_path(database, branch)}", json_body={"query": dict(woql)}
        )
        return result[0] if isinstance(result, list) and result else {}

    async def query_bindings(
        self, database: str, woql: Mapping[str, Any], *, branch: str = MAIN_BRANCH
    ) -> list[dict[str, Any]]:
        response = await self.query(database, woql, branch=branch)
        bindings = response.get("bindings", [])
        return [b for b in bindings if isinstance(b, dict)]


def client_from_env(
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    password_ref: str = "env:TERMINUSDB_ADMIN_PASS",
    ref_name: str = "terminusdb_auth",
    org: str = DEFAULT_ORG,
    user: str = DEFAULT_USER,
    environ: dict[str, str] | None = None,
) -> TerminusClient:
    """Build a client from ``secrets.refs.yaml``-style references.

    Imported lazily so ``terminusdb.py`` stays usable with an explicit config in
    contexts where no environment is configured.
    """
    from integrations.secrets import SecretRef, SecretResolver

    resolver = SecretResolver(environ)
    password = resolver.resolve(SecretRef(name=ref_name, reference=password_ref, required=True))
    assert password is not None  # required=True raises otherwise
    return TerminusClient(TerminusConfig(endpoint=endpoint, user=user, password=password, org=org))
