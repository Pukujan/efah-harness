"""Mutant catalogue — the four evaluation sets of contract Section 17.1.

Contract Section 9.4 sets ``required_kill_rate: 1.0``. Every mutant here is a
*real* mutation, not a description of one:

* **implementation mutants** mutate the subject under test and are run through
  the unmodified oracle, which must return FAIL;
* **evaluator/oracle mutants** mutate the oracle itself -- a subclass with one
  check genuinely removed -- and are killed by the oracle's own fixture suite
  noticing that a known-bad case now passes;
* **test mutants** weaken a visible assertion and are killed by the
  assertion-hash manifest (Section 14.3);
* **workflow/governance mutants** weaken a rule of the harness -- a gate that
  declares a model judge in its verdict path, an auto-merge that ignores a
  requirement, a verifier response that smuggles holdout content -- and are
  killed by the loader, the composite, and the client respectively.

A mutation gate that never kills anything is not a gate (GATE-D3-24). The point
of the evaluator/oracle set in particular is to break the circularity: it is
the only set that tests whether the *assurance* path itself still works when
someone quietly deletes a check from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from governance.states import Verdict
from oracles import fixtures as fx
from oracles.base import Decision, DeterministicOracle, passed
from oracles.oracle_002_lease_fencing import LeaseFencingOracle
from oracles.oracle_003_provenance import ProvenanceBindingOracle, recompute_header_hash


class MutantClass(StrEnum):
    """Contract Section 17.1 evaluation sets."""

    IMPLEMENTATION = "implementation_mutant"
    TEST = "test_mutant"
    EVALUATOR_ORACLE = "evaluator_or_oracle_mutant"
    WORKFLOW_GOVERNANCE = "workflow_or_governance_mutant"


@dataclass
class KillReport:
    killed: bool
    detail: str


@dataclass
class Mutant:
    mutant_id: str
    mutant_class: MutantClass
    target: str
    #: Matches an entry in the oracle definition's ``mutants_killed`` list
    #: where one exists, so a declared mutant with no implementation is visible.
    declared_as: str | None
    description: str
    run: Callable[[dict[str, DeterministicOracle]], KillReport]


# ---------------------------------------------------------------------------
# ORACLE-001 — implementation mutants (mutate the subject, not the oracle)
# ---------------------------------------------------------------------------

def _implementation_mutant(name: str, mutate: Callable[[Any], Any]):
    def run(oracles: dict[str, DeterministicOracle]) -> KillReport:
        oracle = oracles["ORACLE-001"]
        subject = mutate(fx.good_composition())
        decision = oracle.decide(subject)
        killed = decision.verdict is Verdict.FAIL
        return KillReport(
            killed,
            f"{name}: unmodified ORACLE-001 returned {decision.verdict.value}"
            + (f" ({'; '.join(decision.reasons)[:200]})" if decision.reasons else ""),
        )

    return run


def _remove_module_from_composition_root(s):
    s.registered_modules.remove("evaluation")
    return s


def _replace_health_check_with_constant_true(s):
    s.wiring["evaluation"].health_check = "true"
    return s


def _point_e2e_path_at_a_stub(s):
    s.wiring["evaluation"].e2e_path = "stub"
    return s


def _break_entry_point_edge_while_keeping_registration(s):
    s.invocation_edges = [e for e in s.invocation_edges if e != ("tasks", "evaluation")]
    s.import_edges = [e for e in s.import_edges if e != ("tasks", "evaluation")]
    return s


# ---------------------------------------------------------------------------
# ORACLE-002 / ORACLE-003 — evaluator/oracle mutants (mutate the oracle)
# ---------------------------------------------------------------------------

class _NoGenerationComparison(LeaseFencingOracle):
    """Mutant: ``remove_generation_comparison``."""

    def decide(self, subject: Any) -> Decision:
        if subject.submission.generation is not None and subject.lease is not None:
            subject = subject.model_copy(deep=True)
            subject.submission.generation = subject.lease.generation
        return super().decide(subject)


class _ExpiredLeaseIsValid(LeaseFencingOracle):
    """Mutant: ``treat_expired_lease_as_valid``."""

    def decide(self, subject: Any) -> Decision:
        if subject.lease is not None and subject.observed_at is not None:
            subject = subject.model_copy(deep=True)
            subject.lease.expires_at = max(subject.lease.expires_at, subject.observed_at)
        return super().decide(subject)


class _LeaseIdWithoutGeneration(LeaseFencingOracle):
    """Mutant: ``compare_lease_id_without_generation``."""

    def decide(self, subject: Any) -> Decision:
        if subject.lease is not None and subject.submission.lease_id == subject.lease.lease_id:
            subject = subject.model_copy(deep=True)
            subject.submission.generation = subject.lease.generation
            subject.lease.superseded_generations = {}
        return super().decide(subject)


class _AcceptSubmitterTimestamp(LeaseFencingOracle):
    """Mutant: ``accept_submitter_supplied_timestamp``."""

    def decide(self, subject: Any) -> Decision:
        if subject.submission.claimed_submitted_at is not None:
            subject = subject.model_copy(deep=True)
            subject.observed_at = subject.submission.claimed_submitted_at
            subject.submission.claimed_submitted_at = None
        return super().decide(subject)


class _SkipHashRecomputation(ProvenanceBindingOracle):
    """Mutant: ``skip_hash_recomputation`` -- trust the declared hash."""

    def decide(self, subject: Any) -> Decision:
        subject = subject.model_copy(deep=True)
        for result in subject.results:
            result.header["content_hash"] = recompute_header_hash(result.header, result.body)
        return super().decide(subject)


class _AcceptMissingEvidenceField(ProvenanceBindingOracle):
    """Mutant: ``accept_missing_evidence_field``."""

    def decide(self, subject: Any) -> Decision:
        subject = subject.model_copy(deep=True)
        for result in subject.results:
            if not result.evidence_artifacts:
                result.evidence_artifacts = [fx.known_good_evidence_ref()]
        return super().decide(subject)


class _AcceptAnyEvidenceTier(ProvenanceBindingOracle):
    """Mutant: ``accept_any_string_as_evidence_tier``."""

    def decide(self, subject: Any) -> Decision:
        subject = subject.model_copy(deep=True)
        for result in subject.results:
            result.evidence_tier = "DETERMINISTIC_ORACLE"
            result.verdict_path = "deterministic_oracle"
        return super().decide(subject)


class _IgnoreContractVersionStaleness(ProvenanceBindingOracle):
    """Mutant: ``ignore_contract_version_staleness`` -- the check is deleted."""

    def decide(self, subject: Any) -> Decision:
        decision = super().decide(subject)
        remaining = [r for r in decision.reasons if "bound to contract version" not in r]
        if decision.verdict is Verdict.FAIL and not remaining:
            return passed(["staleness suppressed"], unresolvable_reference_count=0)
        decision.reasons = remaining
        return decision


class _ResolveCommitWithoutVerifying(ProvenanceBindingOracle):
    """Mutant: ``resolve_commit_without_verifying_existence``."""

    def decide(self, subject: Any) -> Decision:
        subject = subject.model_copy(deep=True)
        for result in subject.results:
            result.terminus_commit_resolvable = True
            result.repository_commit_resolvable = True
        return super().decide(subject)


ORACLE_MUTANT_CLASSES: dict[str, list[tuple[str, type[DeterministicOracle]]]] = {
    "ORACLE-002": [
        ("remove_generation_comparison", _NoGenerationComparison),
        ("treat_expired_lease_as_valid", _ExpiredLeaseIsValid),
        ("compare_lease_id_without_generation", _LeaseIdWithoutGeneration),
        ("accept_submitter_supplied_timestamp", _AcceptSubmitterTimestamp),
    ],
    "ORACLE-003": [
        ("skip_hash_recomputation", _SkipHashRecomputation),
        ("accept_missing_evidence_field", _AcceptMissingEvidenceField),
        ("accept_any_string_as_evidence_tier", _AcceptAnyEvidenceTier),
        ("ignore_contract_version_staleness", _IgnoreContractVersionStaleness),
        ("resolve_commit_without_verifying_existence", _ResolveCommitWithoutVerifying),
    ],
}


def _oracle_mutant(oracle_id: str, name: str, mutant_class: type[DeterministicOracle]):
    """Kill criterion: the oracle's own fixture suite detects the mutation.

    This is the non-circular part. If a check is silently deleted from an
    oracle, the known-bad fixture that the check existed to catch starts
    passing -- and the suite says so.
    """

    def run(oracles: dict[str, DeterministicOracle]) -> KillReport:
        healthy = oracles[oracle_id]
        mutated = mutant_class(healthy.definition, minted=getattr(healthy, "_minted", {}))
        baseline = fx.run_fixture_suite(healthy)
        if not baseline.ok:
            return KillReport(
                False,
                f"{name}: baseline fixture suite for {oracle_id} is not green, so a kill "
                "would prove nothing",
            )
        mutated_suite = fx.run_fixture_suite(mutated)
        killed = not mutated_suite.ok
        detected_by = [o.fixture_id for o in mutated_suite.failures()]
        return KillReport(
            killed,
            f"{name}: mutated {oracle_id} failed fixtures {detected_by}"
            if killed
            else f"{name}: mutated {oracle_id} still passes its whole fixture suite",
        )

    return run


# ---------------------------------------------------------------------------
# Test mutants and workflow/governance mutants are defined in
# :mod:`mutants.governance_mutants` because they act on the gate layer.
# ---------------------------------------------------------------------------

def implementation_mutants() -> list[Mutant]:
    specs = [
        ("remove_module_from_composition_root", _remove_module_from_composition_root),
        ("replace_health_check_with_constant_true", _replace_health_check_with_constant_true),
        ("point_e2e_path_at_a_stub", _point_e2e_path_at_a_stub),
        (
            "break_entry_point_edge_while_keeping_registration",
            _break_entry_point_edge_while_keeping_registration,
        ),
    ]
    return [
        Mutant(
            mutant_id=f"MUT-IMPL-{index:02d}",
            mutant_class=MutantClass.IMPLEMENTATION,
            target="ORACLE-001",
            declared_as=name,
            description=f"ORACLE-001 declares it kills {name}; this mutation performs it.",
            run=_implementation_mutant(name, mutate),
        )
        for index, (name, mutate) in enumerate(specs, start=1)
    ]


def evaluator_mutants() -> list[Mutant]:
    out: list[Mutant] = []
    index = 1
    for oracle_id, specs in ORACLE_MUTANT_CLASSES.items():
        for name, cls in specs:
            out.append(
                Mutant(
                    mutant_id=f"MUT-ORACLE-{index:02d}",
                    mutant_class=MutantClass.EVALUATOR_ORACLE,
                    target=oracle_id,
                    declared_as=name,
                    description=f"{oracle_id} with '{name}' really removed from its verdict path.",
                    run=_oracle_mutant(oracle_id, name, cls),
                )
            )
            index += 1
    return out


def declared_mutants(definition: dict[str, Any]) -> list[str]:
    return [str(m) for m in (definition.get("mutants_killed") or [])]
