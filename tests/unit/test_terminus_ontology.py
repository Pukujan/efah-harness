"""The control-plane ontology matches contract Sections 9.1 and 9.6.

The entity and edge lists are re-read from ``project-pack/contract.yaml`` rather
than restated here, so this test fails if the ontology and the pack diverge --
which is the drift GATE-D1-02 exists to detect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from governance.envelope import Envelope
from governance.states import ProjectState, TaskState
from ontology import terminus_schema_documents, to_terminus_document
from ontology.jsonld import SchemaGenerationError, _field_type, from_terminus_document
from ontology.schema import (
    ALL_MODELS,
    CONTROL_PLANE_ENTITY_NAMES,
    ENTITY_MODELS,
    ControlPlaneEntity,
    Dependency,
    DependencyEdgeType,
    DependencyKind,
    Project,
    TaskEvent,
    TaskEventType,
)

PACK = Path(__file__).resolve().parents[2] / "project-pack"

#: Contract Section 9.6. Restated only as a tripwire for the pack comparison.
REQUIRED_EDGE_TYPES = {
    "depends_on",
    "blocks",
    "supported_by",
    "derived_from",
    "implemented_by",
    "tested_by",
    "verified_by",
    "evaluated_by",
    "invalidated_by",
    "supersedes",
    "compatible_with",
    "conflicts_with",
    "produced_by",
    "deployed_to",
}


def _contract_yaml() -> dict:
    return yaml.safe_load((PACK / "contract.yaml").read_text())


def test_every_section_9_1_entity_has_a_model():
    declared = _contract_yaml()["control_plane_entities"]
    assert set(declared) == set(CONTROL_PLANE_ENTITY_NAMES)
    assert len(ENTITY_MODELS) == len(declared) == 40


def test_entity_models_are_in_contract_order():
    assert list(CONTROL_PLANE_ENTITY_NAMES) == list(_contract_yaml()["control_plane_entities"])


def test_all_section_9_6_edge_types_exist():
    assert {e.value for e in DependencyEdgeType} == REQUIRED_EDGE_TYPES


def test_all_nine_dependency_planes_exist():
    assert {k.value for k in DependencyKind} == {
        "task",
        "requirement",
        "artifact",
        "software",
        "service",
        "documentation",
        "evaluation",
        "deployment",
        "knowledge",
    }


def test_all_section_9_2_events_exist():
    declared = {
        "TaskCreated",
        "TaskReady",
        "TaskAssigned",
        "LeaseAcquired",
        "LeaseRenewed",
        "WorkerStarted",
        "ToolCallRecorded",
        "ArtifactSubmitted",
        "EvaluationStarted",
        "GatePassed",
        "GateFailed",
        "TaskReworked",
        "TaskBlocked",
        "TaskCompleted",
        "TaskMerged",
        "TaskClosed",
    }
    assert {e.value for e in TaskEventType} == declared


def test_task_state_enum_matches_pack():
    block = _contract_yaml()["task_states"]
    declared = set(block["normal"]) | set(block["exceptional"])
    assert declared == {s.value for s in TaskState}
    # Section 9.3's authority split, as the pack states it.
    assert block["worker_terminal_submission"] == TaskState.CANDIDATE_COMPLETE
    assert block["gate_terminal_pass"] == TaskState.PASSED


def _envelope() -> Envelope:
    return Envelope(schema_id="efah.test", created_by_alias="unit-test")


def test_every_entity_requires_an_envelope():
    for model in ALL_MODELS:
        assert "envelope" in model.model_fields, model.__name__
        with pytest.raises(ValidationError):
            model(entity_id="X-1")


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        Project(
            entity_id="P-1",
            envelope=_envelope(),
            name="n",
            mode="autonomous",
            state=ProjectState.RUNNING,
            pack_manifest_hash="sha256:x",
            smuggled="nope",
        )


@pytest.mark.parametrize("bad", ["has space", "sha256:abc", "a/b", "", "-leading"])
def test_entity_id_must_be_document_safe(bad):
    """TerminusDB percent-encodes a Lexical key, which would break link round-trips."""
    with pytest.raises(ValidationError):
        Project(
            entity_id=bad,
            envelope=_envelope(),
            name="n",
            mode="autonomous",
            state=ProjectState.RUNNING,
            pack_manifest_hash="sha256:x",
        )


def test_document_id_is_class_slash_entity_id():
    project = Project(
        entity_id="EFAH-001",
        envelope=_envelope(),
        name="n",
        mode="autonomous",
        state=ProjectState.RUNNING,
        pack_manifest_hash="sha256:x",
    )
    assert project.document_id == "Project/EFAH-001"


# -- generated JSON-LD ------------------------------------------------------


def test_schema_documents_cover_every_model_plus_the_abstract_parent():
    docs = terminus_schema_documents()
    ids = {d["@id"] for d in docs}
    for model in ALL_MODELS:
        assert model.__name__ in ids
    assert "ControlPlaneEntity" in ids
    assert "Envelope" in ids


def test_abstract_parent_is_abstract_and_children_carry_their_own_key():
    """Measured on 12.0.6: ``@key`` is not inherited, so an omitted key means
    random ids and silently broken links."""
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    assert "@abstract" in docs["ControlPlaneEntity"]
    assert "@key" not in docs["ControlPlaneEntity"]
    for model in ALL_MODELS:
        doc = docs[model.__name__]
        assert doc["@inherits"] == ["ControlPlaneEntity"]
        assert doc["@key"] == {"@type": "Lexical", "@fields": ["entity_id"]}


def test_envelope_is_emitted_as_a_subdocument():
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    envelope = docs["Envelope"]
    assert envelope["@subdocument"] == []
    assert envelope["schema_id"] == "xsd:string"
    assert envelope["terminus_commit"] == {"@type": "Optional", "@class": "xsd:string"}


def test_dependency_endpoints_link_to_the_abstract_parent():
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    assert docs["Dependency"]["source"] == "ControlPlaneEntity"
    assert docs["Dependency"]["target"] == "ControlPlaneEntity"
    assert docs["Dependency"]["edge_type"] == "DependencyEdgeType"


def test_enums_are_emitted_with_their_values():
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    assert set(docs["DependencyEdgeType"]["@value"]) == REQUIRED_EDGE_TYPES
    assert docs["TaskState"]["@type"] == "Enum"


def test_optional_and_list_and_json_mappings():
    """``list`` maps to ``List``: a ``Set`` re-orders on read, which would break
    the content hash the provenance gate recomputes."""
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    task = docs["Task"]
    assert task["allowed_paths"] == {"@type": "List", "@class": "xsd:string"}
    assert task["assigned_alias"] == {"@type": "Optional", "@class": "xsd:string"}
    assert task["project"] == "Project"
    assert docs["WorkUnit"]["success_conditions"] == "sys:JSON"
    assert docs["TaskEvent"]["recorded_at"] == "xsd:dateTime"


def test_unmappable_annotation_raises_rather_than_degrading():
    with pytest.raises(SchemaGenerationError):
        _field_type(complex, {}, {})


def test_instance_document_round_trip():
    event = TaskEvent(
        entity_id="EV-T-010-000001",
        envelope=_envelope(),
        task="Task/T-010",
        sequence=1,
        event_type=TaskEventType.TaskCreated,
        actor_alias="system",
        actor_role="control-plane",
        recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        resulting_state=TaskState.PROPOSED,
        payload={"title": "x"},
    )
    doc = to_terminus_document(event)
    assert doc["@type"] == "TaskEvent"
    assert doc["@id"] == "TaskEvent/EV-T-010-000001"
    assert doc["recorded_at"] == "2026-08-02T00:00:00+00:00"
    assert doc["envelope"]["@type"] == "Envelope"
    assert "lease_generation" not in doc, "None must be omitted, not written as null"

    restored = from_terminus_document(TaskEvent, doc)
    assert restored == event


def test_dependency_document_carries_typed_edge():
    edge = Dependency(
        entity_id="EDGE-1",
        envelope=_envelope(),
        edge_type=DependencyEdgeType.implemented_by,
        kind=DependencyKind.requirement,
        source="Requirement/R-1",
        target="Task/T-1",
    )
    doc = to_terminus_document(edge)
    assert doc["edge_type"] == "implemented_by"
    assert doc["source"] == "Requirement/R-1"


def test_control_plane_entity_is_not_instantiable_as_a_document():
    """It is abstract in the graph; nothing should write one directly."""
    docs = {d["@id"]: d for d in terminus_schema_documents()}
    assert docs["ControlPlaneEntity"]["@abstract"] == []
    assert issubclass(Project, ControlPlaneEntity)
