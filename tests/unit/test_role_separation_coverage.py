"""FINDING-006 — the contract's separations are enforced, not only the pack's.

`model-policy.yaml -> role_incompatibilities` is owner data, and so is the alias
map it constrains. A pair the owner never wrote a rule for was silently
unconstrained: measured, all five binding rules were edges from ``implementer``,
which left every assurance-to-assurance pair unchecked while
``role_separation_findings()`` reported ``none``.

These tests fail if the contract-derived separations stop being enforced, if a
required edge loses its clause reference, or if the current pack ever violates
one. They deliberately do **not** assert that every required edge is declared in
the pack — the point of FINDING-006 is that the contract is enforced whether the
pack declares it or not (§1.2 authority order).
"""

from __future__ import annotations

import pytest
from tests.unit.test_router import all_available, write_policy

from models.errors import RoleConflictError
from models.policy import ModelPolicy, load_model_policy
from models.router import ModelRouter, RoutingRequest
from models.separation import (
    REQUIRED_SEPARATIONS,
    Dimension,
    Strength,
    coverage_report,
    evaluate,
)


@pytest.fixture
def policy() -> ModelPolicy:
    return load_model_policy()


# -- the requirement table itself -----------------------------------------
def test_every_required_separation_cites_a_clause():
    """A separation without a clause is an opinion. §1.2 authority order."""
    for edge in REQUIRED_SEPARATIONS:
        assert edge.contract_ref, f"{edge.left}/{edge.right} cites no clause"
        assert edge.rationale, f"{edge.left}/{edge.right} states no rationale"
        assert edge.left != edge.right


def test_required_separations_are_unique_pairs_per_dimension():
    seen = set()
    for edge in REQUIRED_SEPARATIONS:
        key = (edge.pair, edge.dimension)
        assert key not in seen, f"duplicate requirement for {sorted(edge.pair)}"
        seen.add(key)


def test_the_three_roles_section_12_2_names_are_pairwise_separated():
    """§12.2 names builder, holdout author and adjudicator in one sentence.

    Three roles in one "MUST be distinct" is three edges. The pack declared the
    two touching the implementer; the third is the one FINDING-006 found missing.
    """
    trio = {"implementer", "sealed_holdout_author", "judge"}
    edges = {e.pair for e in REQUIRED_SEPARATIONS if e.pair <= trio}
    assert edges == {
        frozenset({"implementer", "sealed_holdout_author"}),
        frozenset({"implementer", "judge"}),
        frozenset({"sealed_holdout_author", "judge"}),
    }


def test_holdout_author_and_mutant_author_must_differ():
    """DEC-006 rests on this and the pack never declared it.

    The mint refuses a holdout set whose kill rate against its declared mutants
    is below 1.0. One author for both sides makes the kill rate a measure of the
    author's self-consistency reported as assurance.
    """
    edge = next(
        e
        for e in REQUIRED_SEPARATIONS
        if e.pair == frozenset({"sealed_holdout_author", "mutant_author"})
    )
    assert edge.strength is Strength.REQUIRED
    assert edge.dimension is Dimension.AGENT
    assert "DEC-006" in edge.contract_ref


# -- the current pack ------------------------------------------------------
def test_current_pack_satisfies_every_required_separation(policy):
    """Nothing is violated today; FINDING-006 is about enforcement, not breakage."""
    violated = [
        e
        for e in evaluate(policy)
        if e.required.strength is Strength.REQUIRED and e.holds_on_the_current_map is False
    ]
    assert violated == [], [e.as_row() for e in violated]


def test_router_reports_nothing_on_the_current_pack(policy):
    router = ModelRouter(policy=policy, capabilities=all_available(policy))
    assert [f for f in router.role_separation_findings() if not f.startswith("advisory: ")] == []


def test_coverage_report_records_the_star_topology_and_its_caveat(policy):
    report = coverage_report(policy)
    assert report["required_edges"] >= 16
    # Family is a label; FINDING-005 measured three anthropic labels on one pool.
    assert report["family_separation_confidence"] == "label_verified"
    assert "FINDING-005" in report["family_separation_caveat"]


# -- the enforcement actually fires ---------------------------------------
@pytest.mark.parametrize(
    "left,right,clause",
    [
        ("sealed_holdout_author", "mutant_author", "DEC-006"),
        ("adversarial_critic", "judge", "contract_12.4"),
        ("sealed_holdout_author", "judge", "contract_12.2"),
        ("implementer", "oracle_author", "contract_12.2"),
        ("implementer", "integration_verifier", "contract_12.2"),
        ("implementer", "release_verifier", "contract_12.2"),
    ],
)
def test_collapsing_an_undeclared_pair_is_now_a_role_conflict(tmp_path, left, right, clause):
    """Each of these passed silently before FINDING-006: no rule, no finding."""

    def mutate(data):
        data["aliases"][right]["alias"] = data["aliases"][left]["alias"]
        data["aliases"][right]["family"] = data["aliases"][left]["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))

    findings = [f for f in router.role_separation_findings() if not f.startswith("advisory: ")]
    assert any(clause in f for f in findings), findings
    assert any("the pack declares no rule for this pair" in f for f in findings), findings

    with pytest.raises(RoleConflictError):
        router.route(RoutingRequest(role=left))


def test_conditional_clauses_are_advisory_not_blocking(tmp_path):
    """"where feasible" / "where bias is material" is an owner judgment.

    The router reports it and routes; it does not decide materiality.
    """

    def mutate(data):
        data["aliases"]["release_verifier"]["alias"] = data["aliases"]["oracle_author"]["alias"]
        data["aliases"]["release_verifier"]["family"] = data["aliases"]["oracle_author"]["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))

    findings = router.role_separation_findings("oracle_author")
    assert any(f.startswith("advisory: ") and "17.4" in f for f in findings), findings
    assert router.route(RoutingRequest(role="oracle_author")).alias


def test_a_role_scoped_query_only_reports_that_roles_edges(tmp_path):
    def mutate(data):
        data["aliases"]["mutant_author"]["alias"] = data["aliases"]["sealed_holdout_author"]["alias"]
        data["aliases"]["mutant_author"]["family"] = data["aliases"]["sealed_holdout_author"]["family"]

    mutated = write_policy(tmp_path, mutate)
    router = ModelRouter(policy=mutated, capabilities=all_available(mutated))

    assert router.role_separation_findings("mutant_author")
    assert router.role_separation_findings("planner") == []
