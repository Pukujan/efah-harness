"""Contract Section 10.4 -- the twelve checkpoint references, enforced."""

from __future__ import annotations

import pytest

from workflows.state import (
    REQUIRED_CHECKPOINT_FIELDS,
    CheckpointReference,
    MissingCheckpointFields,
    assert_checkpoint_fields,
    carries_graph_state,
    initial_state,
    max_int,
    merge_hashes,
    missing_required_fields,
    union_ordered,
)


def _complete_channels() -> dict:
    return dict(
        initial_state(
            project_id="EFAH-001",
            project_version="1.1",
            contract_version="1.1",
            terminus_database="efah",
            terminus_branch="main",
            terminus_commit="abc123",
            work_unit_id="WU-0001",
            graph_id="task_graph",
        )
    )


def test_required_field_list_matches_contract_section_10_4():
    assert REQUIRED_CHECKPOINT_FIELDS == (
        "project_id",
        "project_version",
        "contract_version",
        "terminus_database",
        "terminus_branch",
        "terminus_commit",
        "work_unit_id",
        "graph_node",
        "lease_generation",
        "input_hashes",
        "output_hashes",
        "pending_gates",
    )


def test_initial_state_satisfies_every_required_field():
    assert missing_required_fields(_complete_channels()) == []
    assert_checkpoint_fields(_complete_channels())


@pytest.mark.parametrize("dropped", REQUIRED_CHECKPOINT_FIELDS)
def test_each_missing_field_is_detected(dropped: str):
    channels = _complete_channels()
    del channels[dropped]
    assert missing_required_fields(channels) == [dropped]
    with pytest.raises(MissingCheckpointFields):
        assert_checkpoint_fields(channels)


@pytest.mark.parametrize("nulled", REQUIRED_CHECKPOINT_FIELDS)
def test_a_null_field_counts_as_missing(nulled: str):
    """``terminus_commit: null`` satisfies a key check and destroys provenance."""
    channels = _complete_channels()
    channels[nulled] = None
    assert missing_required_fields(channels) == [nulled]


def test_framework_only_checkpoint_is_not_asserted_against():
    """The input-staging checkpoint holds no graph state yet."""
    assert carries_graph_state({"__start__": {"a": 1}}) is False
    assert carries_graph_state({"branch:to:node": 1}) is False
    assert_checkpoint_fields({"__start__": {"a": 1}, "branch:to:node": 1})

    # ... but the moment a domain channel appears, all twelve are demanded.
    assert carries_graph_state({"branch:to:node": 1, "project_id": "P"}) is True
    with pytest.raises(MissingCheckpointFields):
        assert_checkpoint_fields({"branch:to:node": 1, "project_id": "P"})


def test_checkpoint_reference_round_trips_the_field_dump():
    ref = CheckpointReference.from_channel_values(_complete_channels())
    dumped = ref.model_dump()
    assert set(dumped) == set(REQUIRED_CHECKPOINT_FIELDS)
    assert dumped["project_id"] == "EFAH-001"


def test_checkpoint_reference_rejects_a_partial_checkpoint():
    channels = _complete_channels()
    del channels["lease_generation"]
    with pytest.raises(KeyError):
        CheckpointReference.from_channel_values(channels)


def test_reducers_merge_concurrent_branches_without_loss():
    assert merge_hashes({"a": "1"}, {"b": "2"}) == {"a": "1", "b": "2"}
    assert merge_hashes({"a": "1"}, {"a": "2"}) == {"a": "2"}
    assert union_ordered(["GATE-1", "GATE-2"], ["GATE-2", "GATE-3"]) == ["GATE-1", "GATE-2", "GATE-3"]
    # Section 9.5: a lease generation never moves backwards.
    assert max_int(4, 2) == 4
    assert max_int(2, 4) == 4
