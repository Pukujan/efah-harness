"""The cached Context7 snapshot satisfies dependency-policy.yaml.

Contract Section 16.1 and ``dependency-policy.yaml -> context7_snapshot_fields``
list eleven required fields per retrieval. GATE-D2-15 checks the hash and the
dependency link. This test checks the file the gate will read, and recomputes
both hashes so a hand-edited snapshot fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from integrations.context7 import verify_snapshot

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "project-pack" / "evidence" / "context7-snapshots"


def _required_fields() -> list[str]:
    policy = yaml.safe_load((ROOT / "project-pack" / "dependency-policy.yaml").read_text())
    return policy["context7_snapshot_fields"]


def _snapshots() -> list[Path]:
    return sorted(SNAPSHOT_DIR.glob("C7-*.json"))


def test_at_least_one_terminusdb_snapshot_is_cached():
    assert any("terminusdb" in p.name for p in _snapshots()), (
        "dependency-policy.yaml sets context7_snapshot_required: true for the selected stack"
    )


@pytest.mark.parametrize("path", _snapshots(), ids=lambda p: p.name)
def test_snapshot_carries_every_required_field(path: Path):
    snapshot = json.loads(path.read_text())
    missing = [field for field in _required_fields() if field not in snapshot]
    assert not missing, f"{path.name} is missing {missing}"


@pytest.mark.parametrize("path", _snapshots(), ids=lambda p: p.name)
def test_snapshot_verifies_against_the_canonical_rule(path: Path):
    """Contract §16.1 requires both hashes but does not define "normalized".

    Two lanes independently chose two different normalisation rules, which is
    worse than either: §16.2's version-diff loop compares normalised documents
    across dependency versions, and two conventions make those diffs
    incomparable. The rule now lives once in integrations.context7 and every
    snapshot is checked against it.
    """
    problems = verify_snapshot(json.loads(path.read_text()))
    assert not problems, f"{path.name}: {problems}"


@pytest.mark.parametrize("path", _snapshots(), ids=lambda p: p.name)
def test_snapshot_is_credential_labelled_and_linked(path: Path):
    snapshot = json.loads(path.read_text())
    assert snapshot["credential_alias"] in {"primary", "secondary"}
    assert snapshot["affected_dependencies"], "a snapshot with no dependency link is unlinked evidence"
    assert snapshot["source_locations"], "Section 7.3 requires the exact supporting location"
    assert snapshot["library_version_or_branch"], "never unpinned"


def test_terminusdb_snapshot_records_the_measured_conflict():
    """Section 16.2's diff loop needs the superseded claim, not just the winner."""
    path = next(p for p in _snapshots() if "terminusdb" in p.name)
    snapshot = json.loads(path.read_text())
    conflicts = snapshot["documentation_vs_measured_conflict"]
    assert conflicts
    branch_list = next(c for c in conflicts if "/api/branch" in c["claim"])
    assert "405" in branch_list["measured_result"]
    assert snapshot["measured_server"]["version"]
