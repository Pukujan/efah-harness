"""Loading oracle definitions from the project pack, and their minted records.

The pack YAMLs are authority (contract Section 1.2) and are read-only here. The
minted record -- content hash, last audit date, the Section 17.4 checklist --
is emitted *alongside* them under ``src/oracles/minted/`` rather than written
back into the pack, because ``acceptance/visible/ASSERTION_HASHES.txt`` pins
the pack's bytes and a builder that edits the pack to make its own gate green
has no gates at all (Section 14.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ORACLE_DEFINITION_DIR = REPO_ROOT / "project-pack" / "acceptance" / "oracle-definitions"
MINTED_DIR = Path(__file__).resolve().parent / "minted"

ORACLE_IDS: tuple[str, ...] = ("ORACLE-001", "ORACLE-002", "ORACLE-003")


class OracleDefinitionMissing(RuntimeError):
    """The pack does not contain a definition the harness depends on."""


def definition_path(oracle_id: str, directory: Path | None = None) -> Path:
    directory = directory or ORACLE_DEFINITION_DIR
    matches = sorted(directory.glob(f"{oracle_id}-*.yaml"))
    if not matches:
        raise OracleDefinitionMissing(f"no definition for {oracle_id} under {directory}")
    return matches[0]


def load_definition(oracle_id: str, directory: Path | None = None) -> dict[str, Any]:
    path = definition_path(oracle_id, directory)
    parsed = yaml.safe_load(path.read_text())
    if not isinstance(parsed, dict):
        raise OracleDefinitionMissing(f"{path} does not parse to a mapping")
    parsed["_source_path"] = str(path.relative_to(REPO_ROOT))
    return parsed


def load_all_definitions(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    return {oid: load_definition(oid, directory) for oid in ORACLE_IDS}


def definition_bytes(oracle_id: str, directory: Path | None = None) -> bytes:
    return definition_path(oracle_id, directory).read_bytes()


def minted_path(oracle_id: str, directory: Path | None = None) -> Path:
    return (directory or MINTED_DIR) / f"{oracle_id}.mint.json"


def load_minted(oracle_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """Return the mint record body, or ``None`` when the oracle is not minted.

    The record is stored as a full :class:`~governance.envelope.CompiledObject`
    so its envelope hash can be verified; callers want the body.
    """
    path = minted_path(oracle_id, directory)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data.get("body", data)


def load_minted_object(oracle_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """The whole compiled object, envelope included, for hash verification."""
    path = minted_path(oracle_id, directory)
    if not path.is_file():
        return None
    return json.loads(path.read_text())
