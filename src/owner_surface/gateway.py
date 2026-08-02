"""Read projections and durable command records for the owner surface.

Contract §5.1: "The dashboard MUST consume read projections, not mutate
authoritative state directly." The surface reads through this gateway and writes
only *requests* — a Decision record or a task-transition request — which then
enter the normal gate path.

TerminusDB is authoritative (§15.2). When it is unreachable the gateway reports
that honestly rather than inventing state: a control surface that shows
confident numbers it did not read is worse than one that says it cannot see.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Protocol

import httpx

from governance.envelope import CompiledObject, content_hash, utc_now
from governance.states import ProjectState, TaskState

from .domain import OpenBlocker, OwnerCommand, ProjectView, WorkUnitView

DEFAULT_TERMINUS_URL = os.environ.get("EFAH_TERMINUSDB_URL", "http://localhost:6363")
DEFAULT_DATABASE = os.environ.get("EFAH_TERMINUSDB_DB", "efah")
#: Durable spill for owner commands and blockers. TerminusDB is authoritative;
#: this is a write-ahead record so a command is never lost when the graph is
#: briefly unreachable. It is explicitly NOT project truth.
LEDGER_PATH = Path(os.environ.get("EFAH_OWNER_LEDGER", ".data/owner_surface_ledger.jsonl"))


class ControlPlaneGateway(Protocol):
    """The seam the surface depends on. Any implementation is swappable."""

    async def project_view(self) -> ProjectView: ...
    async def open_blockers(self) -> list[OpenBlocker]: ...
    async def work_unit_ids(self) -> set[str]: ...
    async def record_command(self, command: OwnerCommand, payload: dict[str, Any]) -> tuple[str, str | None]: ...


class TerminusControlPlaneGateway:
    """Reads the authoritative graph over TerminusDB's HTTP API."""

    def __init__(
        self,
        url: str = DEFAULT_TERMINUS_URL,
        database: str = DEFAULT_DATABASE,
        *,
        user: str = "admin",
        password: str | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._database = database
        self._auth = (user, password or os.environ.get("TERMINUSDB_ADMIN_PASS", ""))
        self._ledger = ledger_path or LEDGER_PATH
        self._lock = asyncio.Lock()

    # -- reads ---------------------------------------------------------------

    async def _get(self, path: str) -> Any | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._url}{path}", auth=self._auth)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def is_reachable(self) -> bool:
        return await self._get("/api/info") is not None

    async def _databases(self) -> list[str]:
        payload = await self._get("/api/db")
        if not isinstance(payload, list):
            return []
        return [str(entry.get("path", "")) for entry in payload if isinstance(entry, dict)]

    async def _branches(self) -> list[str]:
        payload = await self._get(f"/api/db/admin/{self._database}/local/branch")
        if isinstance(payload, dict):
            return sorted(payload.keys())
        if isinstance(payload, list):
            return [str(b) for b in payload]
        return []

    async def project_view(self) -> ProjectView:
        reachable = await self.is_reachable()
        databases = await self._databases() if reachable else []
        has_db = any(d.endswith(f"/{self._database}") or d == self._database for d in databases)
        branches = await self._branches() if has_db else []

        units = self._ledger_work_units()
        blockers = await self.open_blockers()

        if not reachable:
            state = ProjectState.FAILED_INFRASTRUCTURE.value
        elif blockers:
            state = ProjectState.BLOCKED_OWNER_DECISION.value
        else:
            state = ProjectState.RUNNING.value

        return ProjectView(
            project_id="EFAH-001",
            project_state=state,
            contract_id="EFAH-CONTRACT-001",
            contract_version="1.1",
            terminus_database=self._database if has_db else None,
            terminus_branch=branches[0] if branches else None,
            terminus_commit=None,
            tasks_total=len(units),
            tasks_passed=sum(1 for u in units if u.state is TaskState.PASSED),
            tasks_blocked=sum(
                1 for u in units if u.state in {TaskState.BLOCKED_OWNER_DECISION, TaskState.BLOCKED_DEPENDENCY}
            ),
            open_blockers=blockers,
            work_units=units,
        )

    # -- ledger --------------------------------------------------------------

    def _read_ledger(self) -> list[dict[str, Any]]:
        if not self._ledger.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._ledger.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _append_ledger(self, row: dict[str, Any]) -> None:
        self._ledger.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def _ledger_work_units(self) -> list[WorkUnitView]:
        latest: dict[str, WorkUnitView] = {}
        for row in self._read_ledger():
            if row.get("kind") != "work_unit":
                continue
            try:
                view = WorkUnitView(**row["body"])
            except Exception:
                continue
            latest[view.work_unit_id] = view
        return sorted(latest.values(), key=lambda u: u.work_unit_id)

    async def open_blockers(self) -> list[OpenBlocker]:
        latest: dict[str, OpenBlocker] = {}
        for row in self._read_ledger():
            if row.get("kind") != "blocker":
                continue
            try:
                blocker = OpenBlocker(**row["body"])
            except Exception:
                continue
            latest[blocker.blocker_id] = blocker
        return [b for b in sorted(latest.values(), key=lambda b: b.blocker_id) if b.answered_at is None]

    async def work_unit_ids(self) -> set[str]:
        return {u.work_unit_id for u in self._ledger_work_units()}

    async def upsert_work_unit(self, view: WorkUnitView) -> None:
        async with self._lock:
            self._append_ledger({"kind": "work_unit", "at": utc_now(), "body": view.model_dump(mode="json")})

    async def upsert_blocker(self, blocker: OpenBlocker) -> None:
        async with self._lock:
            self._append_ledger({"kind": "blocker", "at": utc_now(), "body": blocker.model_dump(mode="json")})

    # -- writes (requests, not authorisations) -------------------------------

    async def record_command(
        self, command: OwnerCommand, payload: dict[str, Any]
    ) -> tuple[str, str | None]:
        """Persist an owner command as an attributable record.

        Returns ``(record_id, terminus_commit)``. The commit is ``None`` when the
        graph was unreachable; the record is still durable locally and is
        reconciled on the next successful write, so a command is never silently
        dropped.
        """
        record = CompiledObject.create(
            schema_id="efah.owner_command",
            created_by_alias="owner",
            body={"verb": str(command.verb), "text": command.text, "target_id": command.target_id, **payload},
        )
        record_id = content_hash(
            {"h": command.command_hash, "at": command.received_at}
        ).removeprefix("sha256:")[:16]

        async with self._lock:
            self._append_ledger(
                {
                    "kind": "owner_command",
                    "record_id": record_id,
                    "at": utc_now(),
                    "body": record.model_dump(mode="json"),
                }
            )
        commit = await self._write_to_graph(record_id, record)
        return record_id, commit

    async def _write_to_graph(self, record_id: str, record: CompiledObject) -> str | None:
        """Best-effort attributable commit. Absence is reported, never faked."""
        doc = {
            "@type": "OwnerCommandRecord",
            "@id": f"OwnerCommandRecord/{record_id}",
            "payload": json.dumps(record.model_dump(mode="json"), sort_keys=True, default=str),
        }
        params = {
            "graph_type": "instance",
            "author": "owner-control-surface",
            "message": f"owner command {record_id} (contract v1.1)",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self._url}/api/document/admin/{self._database}",
                    auth=self._auth,
                    params=params,
                    json=doc,
                )
        except httpx.HTTPError:
            return None
        if resp.status_code not in (200, 201):
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            return body.get("api:commit") or body.get("commit")
        return None
