"""Oracle minting and hierarchy routing.

Contract Sections 17.3 and 17.4. ``owner_todos.json`` lists ``content_hash``
and ``last_audit_date`` as ``TODO_computed_at_mint`` for all three oracles;
these tests check they were actually computed, that they bind to the pack's
bytes, and that all eleven minting requirements are satisfied rather than
declared.
"""

from __future__ import annotations

import pytest

from governance.envelope import CompiledObject, content_hash
from oracles.base import MINTING_REQUIREMENTS, OracleNotMinted
from oracles.definitions import (
    ORACLE_IDS,
    definition_bytes,
    load_all_definitions,
    load_minted,
    load_minted_object,
)
from oracles.minting import AUDIT_MAX_AGE_DAYS, audit_age_days, mint_all
from oracles.registry import (
    IMPLEMENTATIONS,
    VERDICT_PATH_MODULES,
    HierarchyViolation,
    build_oracles,
    require_minted,
    route,
)


@pytest.fixture(scope="module")
def oracles():
    return build_oracles()


def test_all_three_oracles_are_minted(oracles):
    for oracle_id in ORACLE_IDS:
        record = load_minted(oracle_id)
        assert record is not None, f"{oracle_id} has no mint record"
        assert record["minted"] is True, record["unsatisfied_requirements"]
        require_minted(oracles[oracle_id])


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_the_owner_todo_fields_were_computed_at_mint(oracle_id):
    record = load_minted(oracle_id)
    assert record["content_hash"].startswith("sha256:")
    assert record["content_hash"] != "TODO_computed_at_mint"
    assert record["last_audit_date"] != "TODO_computed_at_mint"
    assert len(record["last_audit_date"]) == 10  # YYYY-MM-DD


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_the_content_hash_binds_the_pack_definition_bytes(oracle_id):
    """A pack edit must invalidate the mint rather than silently surviving it."""
    record = load_minted(oracle_id)
    assert record["content_hash"] == content_hash(definition_bytes(oracle_id))
    assert record["content_hash"] != content_hash(definition_bytes(oracle_id) + b"\n# edited")


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_the_mint_record_is_a_sealed_compiled_object(oracle_id):
    obj = CompiledObject.model_validate(load_minted_object(oracle_id))
    assert obj.is_intact()
    assert obj.envelope.schema_id == "efah.oracle_mint_record"


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_all_eleven_section_17_4_requirements_are_satisfied(oracle_id):
    record = load_minted(oracle_id)
    checked = {c["requirement"] for c in record["requirements"]}
    assert checked == set(MINTING_REQUIREMENTS)
    unsatisfied = [c["requirement"] for c in record["requirements"] if not c["satisfied"]]
    assert unsatisfied == []


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_the_audit_is_not_stale(oracle_id):
    assert audit_age_days(load_minted(oracle_id)) <= AUDIT_MAX_AGE_DAYS


@pytest.mark.parametrize("oracle_id", ORACLE_IDS)
def test_health_is_emitted_with_every_result(oracles, oracle_id):
    """Section 17.4: health with *every* result, not on request."""
    from oracles import fixtures as fx

    oracle = oracles[oracle_id]
    for fixture in fx.fixtures_for(oracle_id):
        if fixture.subject is None:
            continue
        result = oracle.evaluate(fixture.subject, subject_ref=fixture.fixture_id)
        assert sorted(result.health) == sorted(oracle.declared_health_fields)
        assert result.health["content_hash"].startswith("sha256:")


def test_minting_refuses_an_oracle_whose_pinned_suite_is_missing():
    """The gate must be a gate: remove the pinned suite and the mint must fail."""
    definitions = load_all_definitions()
    definitions["ORACLE-002"] = dict(definitions["ORACLE-002"])
    definitions["ORACLE-002"]["pinned_checker_test_suite"] = "tests/contract/does_not_exist.py"
    built = {oid: IMPLEMENTATIONS[oid](definitions[oid]) for oid in ORACLE_IDS}
    records = mint_all(built)
    assert records["ORACLE-002"].minted is False
    assert "pinned_checker_test_suite" in records["ORACLE-002"].unsatisfied
    assert records["ORACLE-001"].minted is True


def test_an_oracle_definition_that_admits_a_judge_cannot_be_constructed():
    definition = dict(load_all_definitions()["ORACLE-001"])
    definition["model_call_in_verdict_path"] = True
    with pytest.raises(OracleNotMinted):
        IMPLEMENTATIONS["ORACLE-001"](definition)

    definition = dict(load_all_definitions()["ORACLE-001"])
    definition["judge_participates"] = True
    with pytest.raises(OracleNotMinted):
        IMPLEMENTATIONS["ORACLE-001"](definition)


def test_an_unminted_oracle_may_not_gate():
    definitions = load_all_definitions()
    unminted = IMPLEMENTATIONS["ORACLE-003"](definitions["ORACLE-003"])
    with pytest.raises(OracleNotMinted):
        require_minted(unminted)


# --- Section 17.3 hierarchy ------------------------------------------------

def test_routing_selects_the_highest_available_level(oracles):
    decision = route("bind evidence to a commit", list(oracles.values()))
    assert decision.selected_level == min(o.hierarchy_level for o in oracles.values())
    assert decision.selected_level == 1
    assert decision.rejected_lower_levels


def test_a_subjective_route_is_refused_while_deterministic_ones_exist(oracles):
    decision = route("anything", list(oracles.values()), allow_subjective=True)
    assert decision.selected_level in {1, 2}


def test_routing_with_no_candidates_selects_nothing_rather_than_guessing():
    decision = route("no oracle for this question", [])
    assert decision.selected_oracle_id is None
    assert decision.selected_level is None


def test_a_downgrade_is_refused(oracles):
    """Section 17.3: a level-2 route while a level-1 oracle is available."""
    from oracles.registry import assert_no_downgrade

    candidates = list(oracles.values())
    downgraded = route("provenance", [oracles["ORACLE-003"]])
    assert downgraded.selected_level == 2
    with pytest.raises(HierarchyViolation):
        assert_no_downgrade(downgraded, candidates)


def test_every_oracle_has_a_declared_verdict_path_module():
    assert set(VERDICT_PATH_MODULES) == set(ORACLE_IDS)
    for oracle_id, module in VERDICT_PATH_MODULES.items():
        assert oracle_id[-3:] in module
