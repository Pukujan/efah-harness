"""GATE-D1-01 A2 and GATE-D1-02: the pack parses and is version-bound."""
from pathlib import Path
import pytest
from integrations.pack import REQUIRED_FILES, PackValidationError, load_pack

PACK = Path(__file__).resolve().parents[2] / "project-pack"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK)


def test_all_eleven_required_files_parse(pack):
    assert set(pack.files) == set(REQUIRED_FILES)


def test_governing_contract_version_is_1_1(pack):
    assert pack.contract_id == "EFAH-CONTRACT-001"
    assert pack.contract_version == "1.1"


def test_manifest_hash_is_stable(pack):
    assert pack.manifest_hash == load_pack(PACK).manifest_hash


def test_every_visible_gate_loads(pack):
    gates = pack.acceptance_gates()
    assert len(gates) == 27
    assert "GATE-D1-10" in gates, "AMENDMENT-001 gate must be present under v1.1"
    for gate_id, gate in gates.items():
        assert gate["model_judge_in_verdict_path"] is False, gate_id


def test_missing_required_file_is_a_typed_blocker(tmp_path):
    """Section 8.1: no silent defaults for material fields."""
    (tmp_path / "contract.yaml").write_text("contract: {id: X, version: '1.0'}\n")
    with pytest.raises(PackValidationError) as exc:
        load_pack(tmp_path)
    assert "missing required file" in str(exc.value)


def test_owner_decisions_are_present(pack):
    docs = pack.owner_documents()
    for required in ("DEC-001-langgraph-supersedes-temporal.md",
                     "DEC-002-eval-gateway-for-gate-bearing-roles.md",
                     "AMENDMENT-001-owner-control-surface.md"):
        assert required in docs
