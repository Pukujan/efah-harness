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
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from composition.inventory import third_party_import_sites
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
    "LOCKFILE_NAME",
    "NO_IMPORT_SITE",
    "UNRESOLVED_VERSION",
    "PackImportResult",
    "compatibility_constraints_for",
    "component_import_prefixes",
    "find_lockfile",
    "import_project_pack",
    "load_lockfile_versions",
    "make_import_branch_name",
    "modules_importing",
]

EFAH_DATABASE = "efah"

#: The resolver output that actually pins versions. ``dependency-policy.yaml``
#: declares *which* components are selected; it does not and cannot pin them --
#: every ``selected_stack`` entry carries ``version: TODO_builder_probe``.
LOCKFILE_NAME = "uv.lock"

#: Carried through verbatim from the pack when no lockfile covers a component,
#: so an unpinned dependency is visibly unpinned in the graph rather than
#: silently presented as pinned (contract Section 8.1: no silent defaults).
UNRESOLVED_VERSION = "TODO_builder_probe"

#: Recorded in ``modules_using`` when the import scan ran and found nothing, so
#: a measured zero is distinguishable from a field nobody populated. Same
#: reasoning as :data:`UNRESOLVED_VERSION`: absence has to be visible.
NO_IMPORT_SITE = "no-first-party-import-site"

#: ``selected_stack`` keys are component nicknames, not PyPI distribution names.
#: Only the aliases that differ need an entry; the rest normalise cleanly.
_COMPONENT_DISTRIBUTIONS = {
    "langgraph_async_sqlite_saver": "langgraph-checkpoint-sqlite",
    "opentelemetry": "opentelemetry-sdk",
    "llamaindex": "llama-index",
}

#: Component -> the dotted import prefixes it publishes, for the components
#: whose name is not itself the import root.
#:
#: Distribution name and import root are different namespaces, and the gap is
#: load-bearing here. Three distributions publish into ``langgraph``:
#: ``langgraph`` owns ``langgraph.graph`` and ``langgraph.pregel``,
#: ``langgraph-checkpoint`` owns ``langgraph.checkpoint``, and
#: ``langgraph-checkpoint-sqlite`` owns ``langgraph.checkpoint.sqlite``. The
#: pack registers the first and the third as separate components, so the third
#: is scoped to the subtree it actually publishes. Giving it the whole root
#: would record every LangGraph importer as a user of the SQLite saver, which
#: is the over-claim this map exists to prevent.
#:
#: An empty tuple means the component publishes no Python import surface at
#: all: ``python`` is the language every module runs on rather than a package
#: any module can import, and ``context7`` is a documentation service reached
#: over HTTP with no client library in this tree.
_COMPONENT_IMPORT_PREFIXES: dict[str, tuple[str, ...]] = {
    "langgraph_async_sqlite_saver": ("langgraph.checkpoint.sqlite",),
    "llamaindex": ("llama_index",),
    "context7": (),
    "python": (),
}

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_SEPARATORS = re.compile(r"[-_.]+")
_SPECIFIER = re.compile(r"[<>=!~]")


def _safe_id(value: str) -> str:
    cleaned = _ID_SAFE.sub("-", value).strip("-")
    return cleaned or "unnamed"


def _normalise_distribution(name: str) -> str:
    """PEP 503 normalisation, so ``inspect_ai`` matches ``inspect-ai``."""
    return _NAME_SEPARATORS.sub("-", name.strip().lower())


def component_import_prefixes(component: str) -> tuple[str, ...]:
    """The dotted import prefixes a ``selected_stack`` component publishes.

    Defaults to the component name in module form -- ``fastapi``, ``pydantic``,
    ``opentelemetry`` and ``inspect_ai`` all normalise cleanly -- and consults
    :data:`_COMPONENT_IMPORT_PREFIXES` only for the names that do not.
    """
    if component in _COMPONENT_IMPORT_PREFIXES:
        return _COMPONENT_IMPORT_PREFIXES[component]
    return (_normalise_distribution(component).replace("-", "_"),)


def modules_importing(component: str, sites: Mapping[str, Sequence[str]]) -> list[str]:
    """First-party modules with a static import of *component*, by dotted name.

    This is the whole of what contract Section 16.3's *modules and contracts
    using it* is answered with here, and the boundary is deliberate.

    * An entry means an ``import`` statement somewhere in ``src/`` names a
      module under one of the component's import prefixes. It is a fact an AST
      sweep proved and any reader can re-derive.
    * :data:`NO_IMPORT_SITE` means the sweep ran and found none. Three
      different situations produce it and this field does not distinguish
      them, because nothing in the source distinguishes them: the component is
      reached over HTTP rather than linked into the process (``litellm``,
      ``terminusdb``, ``plane``, ``phoenix``); it is declared and installed but
      not yet used (``docling``, ``lancedb``, ``inspect_ai``, ``promptfoo``,
      ``llamaindex``); or it is not an importable package at all (``python``,
      ``context7``).

    The HTTP-reached components do have real integration seams --
    ``models/gateway.py``, ``models/router.py``, ``integrations/terminusdb.py``,
    ``integrations/plane.py``, ``integrations/otel.py`` -- and they are
    **deliberately not listed here**. Naming them would answer a different
    question ("which module talks to this service?") with the same field, and
    the list could only be hand-maintained: no import binds those modules to
    those services, so nothing would catch it going stale. A hand-maintained
    list presented as a measurement is precisely the defect this function
    replaces -- ``modules_using`` was the component's own ``selected_stack``
    key, which had the ``langgraph`` entry reporting that it was used by
    ``workflow_runtime``. Those seams are wired in
    :func:`composition.root.build_registry`, whose edges ORACLE-001 checks
    against real imports; that is where a reader should go for them.

    Scope is ``src/`` only. Tests and tools import these packages too, but the
    registry describes the delivered system.
    """
    prefixes = component_import_prefixes(component)
    modules: set[str] = set()
    for imported, importers in sites.items():
        if any(imported == prefix or imported.startswith(f"{prefix}.") for prefix in prefixes):
            modules.update(importers)
    return sorted(modules) or [NO_IMPORT_SITE]


def compatibility_constraints_for(distribution: str, exact_version: str) -> list[str]:
    """Version constraints declared by *distribution*'s own installed metadata.

    Section 16.3 wants *known compatibility constraints*. A distribution's
    ``Requires-Python`` and its specifier-bearing ``Requires-Dist`` lines are
    exactly that, and they are the constraints that actually bite: it is
    ``inspect-ai``'s ``click!=8.2.0,<8.2.2,>=8.1.3`` that decides which click
    this closure can hold, and nothing in the pack records it.

    ``uv.lock`` cannot supply them. It lists each resolved package's
    dependencies by name with the specifiers stripped -- the resolution is
    already done, so the lock has no reason to keep them -- which leaves the
    installed distribution's ``METADATA`` as the only source in the tree.

    Two refusals, both returning ``[]`` rather than a guess:

    * The distribution is not installed. ``litellm``, ``terminusdb``,
      ``plane``, ``phoenix``, ``promptfoo`` and ``llamaindex`` are not Python
      packages in this environment and have no metadata to read.
    * The installed version is not the version this registry entry pins.
      Constraints are version-specific, so attaching one version's
      requirements to another version's pin would be a fabrication of the same
      shape as the lockfile source this module used to claim.

    Requirements gated behind an ``extra`` marker are excluded: an extra that
    was not requested is not in the delivered closure, so its ceilings do not
    bind this build.
    """
    try:
        installed = metadata.metadata(distribution)
    except metadata.PackageNotFoundError:
        return []
    if (installed["Version"] or "") != exact_version:
        return []

    constraints: list[str] = []
    requires_python = installed.get("Requires-Python")
    if requires_python:
        constraints.append(f"Requires-Python: {requires_python}")
    for requirement in installed.get_all("Requires-Dist") or ():
        text = str(requirement).strip()
        name, _, marker = text.partition(";")
        if "extra" in marker and "==" in marker:
            continue
        if _SPECIFIER.search(name):
            constraints.append(text)
    return constraints


@lru_cache(maxsize=1)
def _import_sites() -> dict[str, tuple[str, ...]]:
    """Cached AST sweep of ``src/`` -- one parse of the tree per process."""
    return {imported: tuple(mods) for imported, mods in third_party_import_sites().items()}


def find_lockfile(start: Path) -> Path | None:
    """Walk up from ``start`` looking for the resolver lockfile.

    The pack sits at ``<repo>/project-pack`` and the lockfile at ``<repo>``, so
    the search is upward rather than inside the pack. Returns ``None`` when no
    lockfile exists -- the honest answer for a checkout that has not been
    locked, and the reason the caller must not assume a source.
    """
    for directory in (start, *start.parents):
        candidate = directory / LOCKFILE_NAME
        if candidate.is_file():
            return candidate
    return None


def load_lockfile_versions(lock_path: Path) -> dict[str, str]:
    """Map normalised distribution name -> exact locked version.

    ``uv.lock`` is TOML with one ``[[package]]`` table per resolved
    distribution. Reading it is the only way to answer Section 16.3's
    ``exact_version_and_lockfile_source`` for a Python component: the pack
    declares intent, the lock records the resolution.
    """
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    versions: dict[str, str] = {}
    for package in data.get("package") or ():
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if name and version:
            versions[_normalise_distribution(str(name))] = str(version)
    return versions


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


def build_pack_entities(
    pack: PackFiles,
    *,
    author_alias: str,
    lock_path: Path | None = None,
    import_sites: Mapping[str, Sequence[str]] | None = None,
) -> list[ControlPlaneEntity]:
    """Turn a validated pack into control-plane entities.

    No network and no clock beyond ``imported_at``. It does read two things off
    disk that the pack does not contain -- the resolver lockfile and the import
    graph of ``src/`` -- because Section 16.3 asks for facts the pack cannot
    state about itself. Both are injectable, so a test pins its own inputs
    rather than depending on the checkout layout.

    ``lock_path`` overrides lockfile discovery; ``import_sites`` overrides the
    AST sweep and takes the shape
    :func:`composition.inventory.third_party_import_sites` returns.
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

    # Section 16.3 wants `exact_version_and_lockfile_source`. The pack cannot
    # supply either: every `selected_stack` entry is `version:
    # TODO_builder_probe`, so naming dependency-policy.yaml as the lockfile
    # source asserted a pin that file does not contain. Versions come from the
    # resolver lockfile when one covers the component; when none does -- a
    # non-Python component such as terminusdb, plane or promptfoo, or a Python
    # one that is not in the declared closure -- the entity keeps the pack's
    # unresolved marker and points back at the pack, which is where the only
    # record actually lives.
    #
    # `modules_using` recorded `[str(component)]` -- the selected_stack *key* --
    # so the langgraph entry claimed it was used by "workflow_runtime". That is
    # the dependency's own name spelled a second way, and it answered a
    # different question from the one Section 16.3 asks. It now comes from an
    # AST sweep of src/; `modules_importing` documents what an entry means and
    # what it deliberately omits. `compatibility_constraints` was `[]` on all
    # sixteen and is now read from the pinned distribution's own metadata.
    lockfile = lock_path if lock_path is not None else find_lockfile(pack.root)
    locked_versions = load_lockfile_versions(lockfile) if lockfile is not None else {}
    lockfile_label = lockfile.name if lockfile is not None else None
    sites = _import_sites() if import_sites is None else import_sites
    for component, block in (dependency_policy.get("selected_stack") or {}).items():
        if not isinstance(block, dict):
            continue
        component_name = str(block.get("component", component))
        declared_version = str(block.get("version", UNRESOLVED_VERSION))
        distribution = _COMPONENT_DISTRIBUTIONS.get(
            component_name, _normalise_distribution(component_name)
        )
        locked_version = locked_versions.get(distribution)
        entities.append(
            DependencyVersion(
                entity_id=_safe_id(f"DEP-{component_name}"),
                envelope=_envelope("efah.dependency_version", author_alias),
                component=component_name,
                exact_version=locked_version or declared_version,
                lockfile_source=(
                    lockfile_label
                    if locked_version is not None
                    else "project-pack/dependency-policy.yaml"
                ),
                update_and_rollback_policy=str(
                    (dependency_policy.get("risk_policy") or {}).get(
                        "auto_merge_dependency_updates", "none"
                    )
                ),
                modules_using=modules_importing(component_name, sites),
                compatibility_constraints=(
                    compatibility_constraints_for(distribution, locked_version)
                    if locked_version is not None
                    else []
                ),
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
    lock_path: Path | None = None,
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

    entities = build_pack_entities(pack, author_alias=author_alias, lock_path=lock_path)
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
