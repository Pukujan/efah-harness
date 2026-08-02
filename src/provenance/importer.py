"""Import a project pack onto an isolated TerminusDB branch (GATE-D1-01).

The gate has four assertions and this module produces evidence for all four:

* **A1** *"Import creates a new branch, not a write to main."* -- the branch
  listing and ``main``'s head commit are captured before and after, and
  :attr:`PackImportResult.main_head_unchanged` compares them. Nothing in the
  import path targets ``main``.
* **A2** *"Every required file from Section 6 is present and parsed."* -- carried
  by :func:`integrations.pack.load_pack`, whose manifest is recorded on the
  :class:`~ontology.schema.ProjectPack` entity.
* **A3** *"The import commit is attributable and immutable."* -- the write goes
  through :class:`~provenance.writer.ProvenanceWriter`, which refuses an
  anonymous author and reads the commit record back.
* **A4** *"Contract id and version on the imported Contract entity match the
  pack."* -- both are taken from ``contract.yaml``, never defaulted.

**Blinding.** ``model-policy.yaml`` contains the real vendor and model id behind
each alias. Those two fields are deliberately dropped here: the main graph
receives :class:`~ontology.schema.ModelAlias` entities carrying alias, role and
gateway only. The real identity belongs on the protected instance
(:mod:`integrations.protected_identity`, contract Section 11.2), and importing it
into ``efah`` would defeat GATE-D1-06 in the very first write of the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Sequence

from governance.envelope import (
    CONTRACT_ID,
    CONTRACT_VERSION,
    METHODOLOGY_VERSION,
    Envelope,
    content_hash,
)
from governance.states import ProjectState
from integrations.pack import ProjectPack as PackFiles
from integrations.terminusdb import MAIN_BRANCH, TerminusClient
from ontology.jsonld import terminus_schema_documents
from ontology.schema import (
    Artifact,
    Contract,
    ContractVersion,
    ControlPlaneEntity,
    Decision,
    Dependency,
    DependencyEdgeType,
    DependencyKind,
    DependencyVersion,
    Environment,
    ModelAlias,
    Oracle,
    Project,
    ProjectPack,
    ProjectVersion,
)
from provenance.writer import ProvenanceWriter, WriteReceipt

__all__ = [
    "EFAH_DATABASE",
    "PackImportResult",
    "import_project_pack",
    "make_import_branch_name",
]

EFAH_DATABASE = "efah"

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_id(value: str) -> str:
    cleaned = _ID_SAFE.sub("-", value).strip("-")
    return cleaned or "unnamed"


def make_import_branch_name(manifest_hash: str, *, now: datetime | None = None) -> str:
    """``import-pack-<hash8>-<YYYYmmddTHHMMSSZ>``.

    Measured on TerminusDB 12.0.6: a ``/`` in a branch name is parsed as a path
    separator and rejected with ``api:BadTargetAbsoluteDescriptor``, so the
    conventional ``import/pack-...`` shape is not available.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    short = manifest_hash.removeprefix("sha256:")[:8]
    return f"import-pack-{short}-{stamp}"


def _envelope(schema_id: str, alias: str) -> Envelope:
    return Envelope(schema_id=schema_id, created_by_alias=alias)


def _iso(value: Any, *, fallback: datetime | None = None) -> datetime:
    """Parse a pack date (``2026-08-01``) into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        return fallback or datetime.now(UTC)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback or datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PackImportResult:
    """GATE-D1-01's ``evidence_required`` block, as a value."""

    database: str
    branch: str
    commit_id: str
    branches_before: tuple[str, ...]
    branches_after: tuple[str, ...]
    main_head_before: str | None
    main_head_after: str | None
    database_created: bool
    entity_ids: tuple[str, ...]
    file_manifest: tuple[dict[str, str], ...]
    receipt: WriteReceipt
    schema_commit: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def new_branches(self) -> tuple[str, ...]:
        return tuple(b for b in self.branches_after if b not in self.branches_before)

    @property
    def main_head_unchanged(self) -> bool:
        return self.main_head_before == self.main_head_after

    @property
    def new_branch_present(self) -> bool:
        return self.branch in self.new_branches

    @property
    def is_isolated(self) -> bool:
        """A1 in one property: new branch, main untouched."""
        return self.new_branch_present and self.main_head_unchanged and self.branch != MAIN_BRANCH

    def as_evidence(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "gate_id": "GATE-D1-01",
            "terminusdb_branch_name_and_commit_id": {
                "database": self.database,
                "branch": self.branch,
                "commit_id": self.commit_id,
            },
            "before_after_branch_listing": {
                "before": list(self.branches_before),
                "after": list(self.branches_after),
                "new_branches": list(self.new_branches),
                "main_head_before": self.main_head_before,
                "main_head_after": self.main_head_after,
                "main_head_unchanged": self.main_head_unchanged,
            },
            "import_log_with_file_manifest_and_hashes": [dict(f) for f in self.file_manifest],
            "entity_ids": list(self.entity_ids),
            "database_created_by_this_run": self.database_created,
            "schema_commit": self.schema_commit,
            "write_receipt": self.receipt.as_evidence(),
        }
        payload.update(self.extra)
        payload["evidence_hash"] = content_hash(payload)
        return payload


def build_pack_entities(pack: PackFiles, *, author_alias: str) -> list[ControlPlaneEntity]:
    """Turn a validated pack into control-plane entities.

    Pure: no network, no clock beyond ``imported_at``. That makes the entity set
    unit-testable without a live database, which is why the import path and the
    entity construction are separate functions.
    """
    contract_yaml = pack.yaml("contract.yaml")
    project_yaml = pack.yaml("project.yaml")
    environments_yaml = pack.yaml("environments.yaml")
    model_policy = pack.yaml("model-policy.yaml")
    dependency_policy = pack.yaml("dependency-policy.yaml")

    contract_block = contract_yaml["contract"]
    project_block = project_yaml["project"]
    owner_block = project_yaml.get("owner", {})

    contract_key = str(contract_block["id"])
    contract_version = str(contract_block["version"])
    prepared_at = _iso(contract_block.get("prepared_at"))
    owner_name = str(owner_block.get("name") or owner_block.get("github") or "owner")

    entities: list[ControlPlaneEntity] = []

    contract = Contract(
        entity_id=_safe_id(contract_key),
        envelope=_envelope("efah.contract", author_alias),
        contract_key=contract_key,
        title=str(project_block.get("name", contract_key)),
        current_version=contract_version,
    )
    contract_version_entity = ContractVersion(
        entity_id=_safe_id(f"{contract_key}-v{contract_version}"),
        envelope=_envelope("efah.contract_version", author_alias),
        contract=contract.document_id,
        version=contract_version,
        content_hash=pack.files["contract.md"].content_hash,
        approved_by=owner_name,
        approved_at=prepared_at,
    )
    project_pack = ProjectPack(
        entity_id=_safe_id(f"PACK-{pack.manifest_hash.removeprefix('sha256:')[:12]}"),
        envelope=_envelope("efah.project_pack", author_alias),
        root_path=str(pack.root),
        manifest_hash=pack.manifest_hash,
        required_files_present=True,
        file_manifest={"files": pack.file_manifest()},
        imported_at=datetime.now(UTC),
    )
    project_key = str(project_block["id"])
    project = Project(
        entity_id=_safe_id(project_key),
        envelope=_envelope("efah.project", author_alias),
        name=str(project_block.get("name", project_key)),
        mode="autonomous",
        state=ProjectState.RUNNING,
        pack_manifest_hash=pack.manifest_hash,
        contract=contract.document_id,
        repositories=[
            str(r.get("name") or r.get("repo") or r)
            for r in _as_sequence(pack.yaml("repositories.yaml").get("build_repos"))
        ],
    )
    project_version = ProjectVersion(
        entity_id=_safe_id(f"{project_key}-v{contract_version}"),
        envelope=_envelope("efah.project_version", author_alias),
        project=project.document_id,
        version=contract_version,
        pack_manifest_hash=pack.manifest_hash,
        compiled_at=datetime.now(UTC),
    )
    entities += [contract, contract_version_entity, project_pack, project, project_version]

    for name, block in (environments_yaml.get("environments") or {}).items():
        if not isinstance(block, dict):
            continue
        entities.append(
            Environment(
                entity_id=_safe_id(f"ENV-{name}"),
                envelope=_envelope("efah.environment", author_alias),
                name=str(name),
                kind=str(block.get("description", name)),
                endpoints=_endpoint_map(block),
                is_protected=bool(block.get("sealed") or name == "verifier"),
            )
        )

    gate_bearing_roles = set(
        _as_sequence(
            (model_policy.get("gateway_routing", {}).get("eval", {}) or {}).get("permitted_roles")
        )
    )
    for role, block in (model_policy.get("aliases") or {}).items():
        if not isinstance(block, dict) or "alias" not in block:
            continue
        # NOTE: `litellm_model` and `family` are intentionally not read here.
        entities.append(
            ModelAlias(
                entity_id=_safe_id(str(block["alias"])),
                envelope=_envelope("efah.model_alias", author_alias),
                alias=str(block["alias"]),
                role=str(role),
                gateway=str(block.get("gateway", "production")),
                gate_bearing=str(role) in gate_bearing_roles,
            )
        )

    for component, block in (dependency_policy.get("selected_stack") or {}).items():
        if not isinstance(block, dict):
            continue
        entities.append(
            DependencyVersion(
                entity_id=_safe_id(f"DEP-{block.get('component', component)}"),
                envelope=_envelope("efah.dependency_version", author_alias),
                component=str(block.get("component", component)),
                exact_version=str(block.get("version", "TODO_builder_probe")),
                lockfile_source="project-pack/dependency-policy.yaml",
                update_and_rollback_policy=str(
                    (dependency_policy.get("risk_policy") or {}).get(
                        "auto_merge_dependency_updates", "none"
                    )
                ),
                modules_using=[str(component)],
            )
        )

    for oracle_id, block in sorted(pack.oracle_definitions().items()):
        entities.append(
            Oracle(
                entity_id=_safe_id(oracle_id),
                envelope=_envelope("efah.oracle", author_alias),
                oracle_key=str(block.get("oracle_id", oracle_id)),
                name=str(block.get("name", oracle_id)),
                oracle_type=str(block.get("hierarchy_level", "unspecified")),
                deterministic=bool(block.get("deterministic_verdict_path", False)),
                model_judge_in_verdict_path=bool(block.get("judge_participates", False)),
            )
        )

    decision_dir = pack.root / "evidence" / "owner-documents"
    if decision_dir.is_dir():
        for path in sorted(decision_dir.glob("DEC-*.md")):
            key = path.stem.split("-")[0] + "-" + path.stem.split("-")[1]
            title = _first_heading(path.read_text())
            entities.append(
                Decision(
                    entity_id=_safe_id(key),
                    envelope=_envelope("efah.decision", author_alias),
                    decision_key=key,
                    title=title or path.stem,
                    status="approved",
                    rationale=(
                        f"owner document evidence/owner-documents/{path.name} "
                        f"content_hash={content_hash(path.read_bytes())}"
                    ),
                    decided_by=owner_name,
                    decided_at=prepared_at,
                )
            )

    entities.extend(
        _dependency_edges(
            author_alias=author_alias,
            project=project,
            project_version=project_version,
            project_pack=project_pack,
            contract=contract,
            contract_version=contract_version_entity,
            others=entities,
        )
    )
    return entities


def _dependency_edges(
    *,
    author_alias: str,
    project: Project,
    project_version: ProjectVersion,
    project_pack: ProjectPack,
    contract: Contract,
    contract_version: ContractVersion,
    others: Sequence[ControlPlaneEntity],
) -> list[Dependency]:
    """Section 9.6 edges the pack itself asserts."""
    pairs: list[tuple[DependencyEdgeType, DependencyKind, str, str, str]] = [
        (
            DependencyEdgeType.derived_from,
            DependencyKind.artifact,
            project.document_id,
            project_pack.document_id,
            "the project record is derived from the imported pack",
        ),
        (
            DependencyEdgeType.depends_on,
            DependencyKind.requirement,
            project.document_id,
            contract.document_id,
            "the project is governed by the contract",
        ),
        (
            DependencyEdgeType.supersedes,
            DependencyKind.requirement,
            contract_version.document_id,
            contract.document_id,
            "this contract version is the current head of the contract",
        ),
        (
            DependencyEdgeType.derived_from,
            DependencyKind.requirement,
            project_version.document_id,
            contract_version.document_id,
            "the compiled project version binds to an exact contract version",
        ),
    ]
    for entity in others:
        if isinstance(entity, DependencyVersion):
            pairs.append(
                (
                    DependencyEdgeType.depends_on,
                    DependencyKind.software,
                    project.document_id,
                    entity.document_id,
                    "selected stack component",
                )
            )
        elif isinstance(entity, Oracle):
            pairs.append(
                (
                    DependencyEdgeType.evaluated_by,
                    DependencyKind.evaluation,
                    project.document_id,
                    entity.document_id,
                    "project acceptance is evaluated by this oracle",
                )
            )
        elif isinstance(entity, Environment):
            pairs.append(
                (
                    DependencyEdgeType.deployed_to,
                    DependencyKind.deployment,
                    project.document_id,
                    entity.document_id,
                    "declared environment",
                )
            )

    edges: list[Dependency] = []
    for edge_type, kind, source, target, rationale in pairs:
        key = content_hash({"e": str(edge_type), "s": source, "t": target}).removeprefix("sha256:")[:16]
        edges.append(
            Dependency(
                entity_id=_safe_id(f"EDGE-{key}"),
                envelope=_envelope("efah.dependency", author_alias),
                edge_type=edge_type,
                kind=kind,
                source=source,
                target=target,
                rationale=rationale,
            )
        )
    return edges


def _endpoint_map(block: dict[str, Any]) -> dict[str, Any]:
    endpoints: dict[str, Any] = {}
    for key, value in block.items():
        if isinstance(value, dict) and ("url" in value or "base_url" in value):
            endpoints[key] = value.get("url") or value.get("base_url")
    return endpoints


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return ""


async def import_project_pack(
    client: TerminusClient,
    pack: PackFiles,
    *,
    author_alias: str,
    database: str = EFAH_DATABASE,
    branch: str | None = None,
) -> PackImportResult:
    """Create ``efah`` if absent and import *pack* onto a fresh isolated branch.

    Never writes to ``main``: the schema and the entities both land on the new
    branch, which is what makes A1's ``main_head_unchanged`` true by construction
    rather than by luck.
    """
    database_created = await client.ensure_database(
        database,
        label="EFAH control plane",
        comment=f"Authoritative graph for {CONTRACT_ID} v{CONTRACT_VERSION} (contract Section 15.2)",
    )

    branches_before = tuple(await client.branch_names(database))
    main_head_before = await client.head_commit(database, MAIN_BRANCH)

    branch_name = branch or make_import_branch_name(pack.manifest_hash)
    created = await client.ensure_branch(database, branch_name, origin=MAIN_BRANCH)
    if not created and branch is None:
        raise RuntimeError(f"import branch {branch_name!r} already exists; refusing to reuse it")

    writer = ProvenanceWriter(
        client, database=database, branch=branch_name, author_alias=author_alias
    )
    schema_commit = await writer.ensure_schema(
        terminus_schema_documents(),
        message=f"control-plane ontology for {CONTRACT_ID} v{CONTRACT_VERSION}",
    )

    entities = build_pack_entities(pack, author_alias=author_alias)
    receipt = await writer.write(
        entities,
        message=f"import project pack {pack.manifest_hash} for {pack.project_id}",
        extra_evidence={
            "pack_manifest_hash": pack.manifest_hash,
            "contract_id": pack.contract_id,
            "contract_version": pack.contract_version,
            "methodology_version": METHODOLOGY_VERSION,
        },
    )

    branches_after = tuple(await client.branch_names(database))
    main_head_after = await client.head_commit(database, MAIN_BRANCH)

    result = PackImportResult(
        database=database,
        branch=branch_name,
        commit_id=receipt.materialise_commit,
        branches_before=branches_before,
        branches_after=branches_after,
        main_head_before=main_head_before,
        main_head_after=main_head_after,
        database_created=database_created,
        entity_ids=tuple(e.document_id for e in entities),
        file_manifest=tuple(pack.file_manifest()),
        receipt=receipt,
        schema_commit=schema_commit,
        extra={
            "contract_id_matches_pack": pack.contract_id == CONTRACT_ID,
            "contract_version_on_entity": pack.contract_version,
        },
    )

    # GATE-D1-01 evidence_required is recorded in the authoritative graph rather
    # than only in a file: a JSON file on disk has no commit binding, which is
    # the property Section 18 is asking the evidence to have.
    evidence = result.as_evidence()
    await writer.write(
        [
            Artifact(
                entity_id=_safe_id(f"EV-GATE-D1-01-{result.commit_id[:12]}"),
                envelope=_envelope("efah.artifact", author_alias),
                path=f"terminusdb://{database}/{branch_name}",
                artifact_type="gate_evidence",
                content_hash=evidence["evidence_hash"],
                producer_alias=author_alias,
                storage_location="terminusdb",
                source_input_hashes=[pack.manifest_hash],
            )
        ],
        message=f"GATE-D1-01 import evidence for {branch_name}",
    )
    return result
