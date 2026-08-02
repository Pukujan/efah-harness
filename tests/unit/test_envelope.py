"""Contract Section 8 envelope and Section 18 content binding."""
from governance.envelope import CONTRACT_ID, CONTRACT_VERSION, CompiledObject, content_hash


def test_governing_version_is_1_1():
    """v1.0 as amended by AMENDMENT-001. A stale binding is STALE_CONTRACT_VERSION."""
    assert CONTRACT_ID == "EFAH-CONTRACT-001"
    assert CONTRACT_VERSION == "1.1"


def test_content_hash_is_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_compiled_object_seals_and_verifies():
    obj = CompiledObject.create(schema_id="efah.test", created_by_alias="implementer-i12",
                                body={"requirement_id": "R-001"})
    assert obj.envelope.content_hash.startswith("sha256:")
    assert obj.is_intact()


def test_tampered_body_is_detected():
    """Section 18: artifacts are mechanically bound to their content."""
    obj = CompiledObject.create(schema_id="efah.test", created_by_alias="implementer-i12",
                                body={"requirement_id": "R-001"})
    obj.body["requirement_id"] = "R-002"
    assert not obj.is_intact()


def test_envelope_carries_every_section_8_field():
    obj = CompiledObject.create(schema_id="efah.test", created_by_alias="planner-p04", body={})
    for field in ("schema_id", "schema_version", "contract_id", "contract_version",
                  "methodology_version", "terminus_database", "terminus_branch",
                  "terminus_commit", "content_hash", "created_by_alias", "created_at"):
        assert field in obj.envelope.model_dump()
