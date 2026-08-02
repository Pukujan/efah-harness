"""Mutation gate runner.

Contract Section 9.4: ``required_kill_rate: 1.0``. GATE-D3-24: *a mutation gate
that never kills anything is not a gate.*

The runner reports two things a mutation score alone cannot: which declared
mutants have no implementation behind them (an oracle definition that lists
``mutants_killed`` entries nobody wrote is claiming a kill it never made), and
which of the four Section 17.1 sets are represented. A 100% kill rate over one
set is not a mutation gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evaluation.binding import CandidateBinding, Lane, LaneRun
from governance.envelope import CompiledObject
from governance.states import TaskState, Verdict
from mutants.catalog import (
    KillReport,
    Mutant,
    MutantClass,
    declared_mutants,
    evaluator_mutants,
    implementation_mutants,
)
from mutants.governance_mutants import governance_mutants, test_mutants
from oracles.base import DeterministicOracle

REQUIRED_KILL_RATE = 1.0

#: Contract Section 17.1. All four sets must be represented.
REQUIRED_SETS: tuple[MutantClass, ...] = (
    MutantClass.IMPLEMENTATION,
    MutantClass.TEST,
    MutantClass.EVALUATOR_ORACLE,
    MutantClass.WORKFLOW_GOVERNANCE,
)


def all_mutants() -> list[Mutant]:
    return implementation_mutants() + evaluator_mutants() + test_mutants() + governance_mutants()


@dataclass
class MutantOutcome:
    mutant_id: str
    mutant_class: MutantClass
    target: str
    declared_as: str | None
    killed: bool
    detail: str
    error: str | None = None


@dataclass
class MutationRunResult:
    candidate_commit: str
    outcomes: list[MutantOutcome] = field(default_factory=list)
    undeclared_but_implemented: list[str] = field(default_factory=list)
    declared_but_unimplemented: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def killed(self) -> int:
        return sum(1 for o in self.outcomes if o.killed)

    @property
    def survivors(self) -> list[MutantOutcome]:
        return [o for o in self.outcomes if not o.killed]

    @property
    def kill_rate(self) -> float:
        return (self.killed / self.total) if self.total else 0.0

    @property
    def sets_covered(self) -> list[MutantClass]:
        return sorted({o.mutant_class for o in self.outcomes}, key=lambda c: c.value)

    def verdict(self) -> Verdict:
        if not self.outcomes:
            return Verdict.UNVERIFIABLE
        if self.declared_but_unimplemented:
            return Verdict.FAIL
        if set(self.sets_covered) != set(REQUIRED_SETS):
            return Verdict.FAIL
        return Verdict.PASS if self.kill_rate >= REQUIRED_KILL_RATE else Verdict.FAIL

    def failure_state(self) -> TaskState | None:
        return None if self.verdict() is Verdict.PASS else TaskState.FAILED_MUTATION

    def lane_run(self) -> LaneRun:
        return LaneRun(
            lane=Lane.MUTANT,
            candidate_commit=self.candidate_commit,
            verdict=self.verdict(),
            detail=f"{self.killed}/{self.total} killed",
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "mutation_gate",
            "expected": f"kill_rate >= {REQUIRED_KILL_RATE}",
            "candidate_commit": self.candidate_commit,
            "total": self.total,
            "killed": self.killed,
            "kill_rate": round(self.kill_rate, 4),
            "required_kill_rate": REQUIRED_KILL_RATE,
            "verdict": self.verdict().value,
            "sets_required": [c.value for c in REQUIRED_SETS],
            "sets_covered": [c.value for c in self.sets_covered],
            "declared_but_unimplemented": self.declared_but_unimplemented,
            "undeclared_but_implemented": self.undeclared_but_implemented,
            "mutants": [
                {
                    "mutant_id": o.mutant_id,
                    "set": o.mutant_class.value,
                    "target": o.target,
                    "declared_as": o.declared_as,
                    "killed": o.killed,
                    "detail": o.detail,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
            "survivors": [o.mutant_id for o in self.survivors],
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.mutation_run",
            created_by_alias="mutant-m03",
            body=self.as_evidence(),
        )


def _coverage(
    mutants: list[Mutant], oracles: dict[str, DeterministicOracle]
) -> tuple[list[str], list[str]]:
    declared: set[str] = set()
    for oracle_id, oracle in oracles.items():
        declared |= {f"{oracle_id}:{name}" for name in declared_mutants(oracle.definition)}
    implemented = {
        f"{m.target}:{m.declared_as}" for m in mutants if m.declared_as is not None
    }
    return sorted(implemented - declared), sorted(declared - implemented)


def run_mutation_gate(
    oracles: dict[str, DeterministicOracle],
    binding: CandidateBinding,
    mutants: list[Mutant] | None = None,
) -> MutationRunResult:
    """Run every mutant against *this exact* candidate commit (GATE-D2-19)."""
    catalogue = mutants if mutants is not None else all_mutants()
    result = MutationRunResult(candidate_commit=binding.commit_sha)
    undeclared, unimplemented = _coverage(catalogue, oracles)
    result.undeclared_but_implemented = undeclared
    result.declared_but_unimplemented = unimplemented

    for mutant in catalogue:
        try:
            report: KillReport = mutant.run(oracles)
        except Exception as exc:  # noqa: BLE001 - a crashing mutant is not a killed one
            result.outcomes.append(
                MutantOutcome(
                    mutant.mutant_id,
                    mutant.mutant_class,
                    mutant.target,
                    mutant.declared_as,
                    killed=False,
                    detail="mutant run raised",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        result.outcomes.append(
            MutantOutcome(
                mutant.mutant_id,
                mutant.mutant_class,
                mutant.target,
                mutant.declared_as,
                killed=report.killed,
                detail=report.detail,
            )
        )
    return result
