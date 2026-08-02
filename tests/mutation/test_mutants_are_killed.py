"""The mutation gate. Contract Sections 9.4, 17.1; GATE-D3-24.

``required_kill_rate: 1.0``. A surviving mutant is a hole in the assurance
path, so these tests fail on any survivor rather than reporting a score.

The most important test in this file is
:func:`test_a_deliberately_broken_oracle_is_caught_by_its_own_fixture_suite`.
Everything else checks that the harness rejects bad *candidates*; that one
checks the harness rejects a bad *harness*, which is the only way out of the
circularity where the thing doing the checking is the thing being checked.
"""

from __future__ import annotations

import pytest

from evaluation.binding import CandidateBinding
from governance.states import Verdict
from mutants.catalog import MutantClass, evaluator_mutants, implementation_mutants
from mutants.governance_mutants import governance_mutants
from mutants.governance_mutants import test_mutants as build_test_mutants
from mutants.runner import REQUIRED_KILL_RATE, all_mutants, run_mutation_gate
from oracles import fixtures as fx
from oracles.registry import build_oracles


@pytest.fixture(scope="module")
def oracles():
    return build_oracles()


@pytest.fixture(scope="module")
def binding():
    return CandidateBinding.from_head()


@pytest.fixture(scope="module")
def mutation(oracles, binding):
    return run_mutation_gate(oracles, binding)


def test_every_mutant_is_killed(mutation):
    survivors = [f"{o.mutant_id} ({o.declared_as}): {o.detail}" for o in mutation.survivors]
    assert not survivors, survivors
    assert mutation.kill_rate >= REQUIRED_KILL_RATE


def test_all_four_section_17_1_sets_are_represented(mutation):
    """A 100% kill rate over one set is not a mutation gate."""
    assert set(mutation.sets_covered) == {
        MutantClass.IMPLEMENTATION,
        MutantClass.TEST,
        MutantClass.EVALUATOR_ORACLE,
        MutantClass.WORKFLOW_GOVERNANCE,
    }


def test_the_gate_actually_kills_something(mutation):
    """A mutation gate that never kills anything is not a gate (GATE-D3-24)."""
    assert mutation.total >= 4
    assert mutation.killed == mutation.total
    assert mutation.verdict() is Verdict.PASS


def test_every_declared_mutant_has_an_implementation(mutation):
    """An oracle listing mutants nobody wrote is claiming a kill it never made."""
    assert mutation.declared_but_unimplemented == []


@pytest.mark.parametrize("mutant", implementation_mutants(), ids=lambda m: m.mutant_id)
def test_each_implementation_mutant_is_rejected_by_the_unmodified_oracle(mutant, oracles):
    report = mutant.run(oracles)
    assert report.killed, f"{mutant.declared_as}: {report.detail}"


@pytest.mark.parametrize("mutant", evaluator_mutants(), ids=lambda m: m.mutant_id)
def test_each_oracle_mutant_is_caught_by_the_fixture_suite(mutant, oracles):
    report = mutant.run(oracles)
    assert report.killed, f"{mutant.declared_as}: {report.detail}"


@pytest.mark.parametrize("mutant", build_test_mutants(), ids=lambda m: m.mutant_id)
def test_each_test_mutant_is_caught_by_the_assertion_hash_manifest(mutant, oracles):
    report = mutant.run(oracles)
    assert report.killed, report.detail


@pytest.mark.parametrize("mutant", governance_mutants(), ids=lambda m: m.mutant_id)
def test_each_governance_mutant_is_refused_by_the_harness(mutant, oracles):
    report = mutant.run(oracles)
    assert report.killed, report.detail


def test_a_deliberately_broken_oracle_is_caught_by_its_own_fixture_suite(oracles):
    """The non-circularity check, stated as plainly as it can be.

    Delete the generation comparison from ORACLE-002 and its known-bad fixture
    KB-002 -- "worker holds generation 3 while current generation is 4" --
    stops failing. That flip is the kill.
    """
    from mutants.catalog import ORACLE_MUTANT_CLASSES

    healthy = oracles["ORACLE-002"]
    assert fx.run_fixture_suite(healthy).ok, "the baseline suite must be green first"

    name, mutant_class = ORACLE_MUTANT_CLASSES["ORACLE-002"][0]
    assert name == "remove_generation_comparison"
    broken = mutant_class(healthy.definition)
    suite = fx.run_fixture_suite(broken)

    assert not suite.ok, "removing the generation comparison went undetected"
    detected = {o.fixture_id for o in suite.failures()}
    assert "KB-002" in detected, f"detected by {detected}, expected KB-002 among them"


def test_the_runner_reports_a_crashing_mutant_as_surviving(oracles, binding):
    """A mutant that blows up the harness has not been killed by it."""
    from mutants.catalog import KillReport, Mutant

    def explode(_: dict) -> KillReport:
        raise RuntimeError("boom")

    exploding = Mutant(
        mutant_id="MUT-CRASH-01",
        mutant_class=MutantClass.IMPLEMENTATION,
        target="ORACLE-001",
        declared_as=None,
        description="a mutant whose runner raises",
        run=explode,
    )
    result = run_mutation_gate(oracles, binding, mutants=[exploding])
    assert result.survivors
    assert result.outcomes[0].error is not None
    assert result.verdict() is not Verdict.PASS


def test_the_catalogue_is_not_empty():
    assert len(all_mutants()) >= 20
