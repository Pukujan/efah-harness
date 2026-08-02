"""Read/write access to the project side of the authoritative graph.

Everything here goes through :class:`~provenance.writer.ProvenanceWriter`, so a
project state change is a commit with an author, not an in-memory mutation.

Contract Section 6.2 closes the set of terminal project states. This repository
refuses to move a project *out* of a terminal state: "a run cannot end in an
ambiguous mostly-done" also means it cannot quietly un-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from governance.states import TERMINAL_PROJECT_STATES, ProjectState, TaskState
from ontology.schema import (
    Contract,
    ContractVersion,
    Dependency,
    Project,
    ProjectPack,
    ProjectVersion,
    Task,
    Workstream,
)
from provenance.writer import ProvenanceWriter, WriteReceipt

__all__ = ["ProjectRepository", "ProjectSummary", "TerminalStateViolation"]


class TerminalStateViolation(RuntimeError):
    """An attempt to move a project out of a Section 6.2 terminal state."""


@dataclass(frozen=True)
class ProjectSummary:
    """A cheap, honest view of what is actually in the graph."""

    project_id: str
    state: ProjectState
    pack_manifest_hash: str
    contract_id: str | None
    contract_version: str | None
    entity_counts: dict[str, int]
    task_states: dict[str, int]
    dependency_edges: dict[str, int]
    terminus_database: str
    terminus_branch: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": str(self.state),
            "pack_manifest_hash": self.pack_manifest_hash,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "entity_counts": dict(self.entity_counts),
            "task_states": dict(self.task_states),
            "dependency_edges": dict(self.dependency_edges),
            "terminus_database": self.terminus_database,
            "terminus_branch": self.terminus_branch,
        }


class ProjectRepository:
    def __init__(self, writer: ProvenanceWriter) -> None:
        self._writer = writer

    @property
    def writer(self) -> ProvenanceWriter:
        return self._writer

    async def get_project(self, entity_id: str) -> Project | None:
        result = await self._writer.read(Project, entity_id)
        return result if isinstance(result, Project) else None

    async def list_projects(self) -> list[Project]:
        return [p for p in await self._writer.read_all(Project) if isinstance(p, Project)]

    async def list_tasks(self, project_document_id: str | None = None) -> list[Task]:
        tasks = [t for t in await self._writer.read_all(Task) if isinstance(t, Task)]
        if project_document_id is None:
            return tasks
        return [t for t in tasks if t.project == project_document_id]

    async def list_workstreams(self, project_document_id: str | None = None) -> list[Workstream]:
        streams = [w for w in await self._writer.read_all(Workstream) if isinstance(w, Workstream)]
        if project_document_id is None:
            return streams
        return [w for w in streams if w.project == project_document_id]

    async def set_state(
        self, entity_id: str, state: ProjectState, *, reason: str
    ) -> tuple[Project, WriteReceipt]:
        project = await self.get_project(entity_id)
        if project is None:
            raise KeyError(f"no Project/{entity_id} in {self._writer.database}/{self._writer.branch}")
        if project.state in TERMINAL_PROJECT_STATES and state != project.state:
            raise TerminalStateViolation(
                f"Project/{entity_id} is terminal in {project.state}; "
                f"contract Section 6.2 does not permit a transition to {state}"
            )
        updated = project.model_copy(update={"state": state})
        receipt = await self._writer.write(
            [updated],
            message=f"project {entity_id} -> {state}: {reason}",
            upsert=True,
        )
        return updated, receipt

    async def summary(self, entity_id: str) -> ProjectSummary:
        project = await self.get_project(entity_id)
        if project is None:
            raise KeyError(f"no Project/{entity_id} in {self._writer.database}/{self._writer.branch}")

        counts: dict[str, int] = {}
        for model in (Project, ProjectVersion, ProjectPack, Contract, ContractVersion, Task, Workstream):
            counts[model.__name__] = len(await self._writer.read_all(model))

        tasks = await self.list_tasks(project.document_id)
        task_states: dict[str, int] = {}
        for task in tasks:
            key = str(task.state)
            task_states[key] = task_states.get(key, 0) + 1

        edges = [d for d in await self._writer.read_all(Dependency) if isinstance(d, Dependency)]
        edge_counts: dict[str, int] = {}
        for edge in edges:
            key = str(edge.edge_type)
            edge_counts[key] = edge_counts.get(key, 0) + 1

        contract_id = None
        contract_version = None
        if project.contract:
            contract = await self._writer.read(Contract, project.contract.split("/", 1)[-1])
            if isinstance(contract, Contract):
                contract_id = contract.contract_key
                contract_version = contract.current_version

        return ProjectSummary(
            project_id=project.entity_id,
            state=project.state,
            pack_manifest_hash=project.pack_manifest_hash,
            contract_id=contract_id,
            contract_version=contract_version,
            entity_counts=counts,
            task_states=task_states or {str(TaskState.PROPOSED): 0},
            dependency_edges=edge_counts,
            terminus_database=self._writer.database,
            terminus_branch=self._writer.branch,
        )
