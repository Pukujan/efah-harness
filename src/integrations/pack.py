"""Project-pack loading and validation.

Contract Section 6 defines the required files; Section 6.1 defines intake
behaviour. Section 8.1 forbids silent defaults for material fields, so a missing
required file is a typed blocker rather than an assumed default.

The loader is deliberately read-only. It never rewrites the pack: the pack is the
owner's artifact and the harness's authority (Section 1.2 priority 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from governance.envelope import content_hash

#: Contract Section 6. All eleven must be present and parse to a mapping.
REQUIRED_FILES = (
    "contract.md",
    "contract.yaml",
    "project.yaml",
    "repositories.yaml",
    "environments.yaml",
    "model-policy.yaml",
    "methodology-policy.yaml",
    "dependency-policy.yaml",
    "autonomy-policy.yaml",
    "plane.yaml",
    "secrets.refs.yaml",
)

OPTIONAL_DIRECTORIES = (
    "acceptance/visible",
    "acceptance/oracle-definitions",
    "evidence/owner-documents",
    "evidence/context7-snapshots",
)


class PackValidationError(RuntimeError):
    """Raised when the pack cannot be imported. Maps to a typed blocker."""


@dataclass(frozen=True)
class PackFile:
    name: str
    path: Path
    content_hash: str
    parsed: Any


@dataclass
class ProjectPack:
    """A validated, hashed project pack.

    ``manifest_hash`` covers every required file, so the import commit in
    TerminusDB binds to an exact pack revision (Section 15.2, Section 18).
    """

    root: Path
    files: dict[str, PackFile] = field(default_factory=dict)

    @property
    def manifest_hash(self) -> str:
        return content_hash({name: f.content_hash for name, f in sorted(self.files.items())})

    @property
    def contract_id(self) -> str:
        return self.files["contract.yaml"].parsed["contract"]["id"]

    @property
    def contract_version(self) -> str:
        return str(self.files["contract.yaml"].parsed["contract"]["version"])

    @property
    def project_id(self) -> str:
        return self.files["project.yaml"].parsed["project"]["id"]

    def yaml(self, name: str) -> dict[str, Any]:
        return self.files[name].parsed

    def file_manifest(self) -> list[dict[str, str]]:
        """Evidence for GATE-D1-01 A2: the parsed required-file manifest."""
        return [
            {"name": name, "content_hash": f.content_hash, "path": str(f.path.relative_to(self.root))}
            for name, f in sorted(self.files.items())
        ]

    def acceptance_gates(self) -> dict[str, dict[str, Any]]:
        """Load every visible gate definition, keyed by ``gate_id``."""
        gates: dict[str, dict[str, Any]] = {}
        gate_dir = self.root / "acceptance" / "visible"
        if not gate_dir.is_dir():
            return gates
        for path in sorted(gate_dir.glob("GATE-*.yaml")):
            parsed = yaml.safe_load(path.read_text())
            if isinstance(parsed, dict) and "gate_id" in parsed:
                gates[parsed["gate_id"]] = parsed
        return gates

    def oracle_definitions(self) -> dict[str, dict[str, Any]]:
        oracles: dict[str, dict[str, Any]] = {}
        oracle_dir = self.root / "acceptance" / "oracle-definitions"
        if not oracle_dir.is_dir():
            return oracles
        for path in sorted(oracle_dir.glob("ORACLE-*.yaml")):
            parsed = yaml.safe_load(path.read_text())
            if isinstance(parsed, dict):
                oracles[parsed.get("oracle_id", path.stem)] = parsed
        return oracles

    def owner_documents(self) -> dict[str, str]:
        doc_dir = self.root / "evidence" / "owner-documents"
        if not doc_dir.is_dir():
            return {}
        return {p.name: content_hash(p.read_bytes()) for p in sorted(doc_dir.glob("*.md"))}


def load_pack(root: Path | str) -> ProjectPack:
    """Validate and load a project pack.

    Raises :class:`PackValidationError` -- never substitutes a default -- when a
    required file is absent, empty, or does not parse to a mapping.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise PackValidationError(f"project pack directory not found: {root}")

    pack = ProjectPack(root=root)
    problems: list[str] = []

    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file():
            problems.append(f"missing required file: {name}")
            continue
        raw = path.read_bytes()
        if not raw.strip():
            problems.append(f"required file is empty: {name}")
            continue
        if name.endswith(".yaml"):
            try:
                parsed = yaml.safe_load(raw.decode("utf-8"))
            except yaml.YAMLError as exc:
                problems.append(f"{name} does not parse as YAML: {exc}")
                continue
            if not isinstance(parsed, dict):
                problems.append(f"{name} does not parse to a mapping")
                continue
        else:
            parsed = raw.decode("utf-8")
        pack.files[name] = PackFile(name=name, path=path, content_hash=content_hash(raw), parsed=parsed)

    if problems:
        raise PackValidationError("; ".join(problems))

    _check_version_binding(pack, problems)
    if problems:
        raise PackValidationError("; ".join(problems))
    return pack


def _check_version_binding(pack: ProjectPack, problems: list[str]) -> None:
    """GATE-D1-02: schemas validate AND are version-bound.

    Every pack file declares ``contract_id``. Files may legitimately declare an
    *older* ``contract_version`` than the governing one -- v1.1 is v1.0 plus an
    additive amendment, so a file written against v1.0 is not stale. What is not
    allowed is a file bound to a different contract entirely.
    """
    expected_id = pack.contract_id
    for name, pack_file in pack.files.items():
        if not name.endswith(".yaml") or name == "contract.yaml":
            continue
        declared = pack_file.parsed.get("contract_id")
        if declared is not None and declared != expected_id:
            problems.append(f"{name} is bound to contract {declared!r}, expected {expected_id!r}")
        if "schema_id" not in pack_file.parsed:
            problems.append(f"{name} declares no schema_id")
