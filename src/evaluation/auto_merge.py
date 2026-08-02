"""The thirteen auto-merge requirements, evaluated as one composite.

Contract Section 21.2. GATE-D3-25 A1 requires all thirteen to be *evaluated and
recorded per PR* -- not thirteen separate green checkmarks somewhere, one
record that names each requirement, its source, and its result.

Section 21.2 also says the implementing agent does not self-certify, and that a
green mergeable PR MUST NOT wait for an additional human message. Both halves
matter: this composite refuses to merge on a missing requirement, and it also
refuses to treat "a human has not said go" as a reason to block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governance.envelope import CompiledObject
from governance.states import Verdict

#: Contract Section 21.2, verbatim and in order. Thirteen. Not twelve.
AUTO_MERGE_REQUIREMENTS: tuple[str, ...] = (
    "contract_unchanged_or_approved",
    "unresolved_scope_drift",
    "visible_tests",
    "integration_tests",
    "composition_test",
    "hidden_holdout",
    "mutation_gate",
    "oracle_health",
    "provenance_gate",
    "dependency_policy",
    "unresolved_high_risk_findings",
    "protected_assets_accessed",
    "branch_up_to_date",
)

#: The required value for each. Booleans and counts are not all "PASS".
REQUIRED_VALUES: dict[str, Any] = {
    "contract_unchanged_or_approved": True,
    "unresolved_scope_drift": 0,
    "visible_tests": "PASS",
    "integration_tests": "PASS",
    "composition_test": "PASS",
    "hidden_holdout": "PASS",
    "mutation_gate": "PASS",
    "oracle_health": "PASS",
    "provenance_gate": "PASS",
    "dependency_policy": "PASS",
    "unresolved_high_risk_findings": 0,
    "protected_assets_accessed": False,
    "branch_up_to_date": True,
}


class RequirementNotEvaluated(RuntimeError):
    """GATE-D3-25 A1. An unevaluated requirement is not a satisfied one."""


@dataclass
class RequirementRecord:
    name: str
    required: Any
    observed: Any
    source: str
    evaluated: bool = True

    @property
    def satisfied(self) -> bool:
        return self.evaluated and self.observed == self.required

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.name,
            "required": self.required,
            "observed": self.observed,
            "satisfied": self.satisfied,
            "evaluated": self.evaluated,
            "source": self.source,
        }


@dataclass
class AutoMergeEvaluation:
    """One record per PR, carrying all thirteen requirements."""

    pull_request_ref: str
    candidate_commit: str
    records: dict[str, RequirementRecord] = field(default_factory=dict)
    merge_actor: str | None = None
    implementing_agent_alias: str | None = None
    ci_checks_present: bool = False

    def record(self, name: str, observed: Any, source: str) -> None:
        if name not in REQUIRED_VALUES:
            raise RequirementNotEvaluated(
                f"{name!r} is not one of the thirteen Section 21.2 requirements"
            )
        self.records[name] = RequirementRecord(
            name=name, required=REQUIRED_VALUES[name], observed=observed, source=source
        )

    def record_not_evaluated(self, name: str, source: str) -> None:
        """Honest absence. Not the same as a failure, and never the same as a pass."""
        if name not in REQUIRED_VALUES:
            raise RequirementNotEvaluated(f"{name!r} is not a Section 21.2 requirement")
        self.records[name] = RequirementRecord(
            name=name,
            required=REQUIRED_VALUES[name],
            observed=None,
            source=source,
            evaluated=False,
        )

    @property
    def missing(self) -> list[str]:
        return [name for name in AUTO_MERGE_REQUIREMENTS if name not in self.records]

    @property
    def not_evaluated(self) -> list[str]:
        return [name for name, rec in self.records.items() if not rec.evaluated]

    @property
    def unsatisfied(self) -> list[str]:
        return [
            name
            for name in AUTO_MERGE_REQUIREMENTS
            if name in self.records and not self.records[name].satisfied
        ]

    @property
    def all_thirteen_recorded(self) -> bool:
        return not self.missing

    def verdict(self) -> Verdict:
        """A composite. One unsatisfied requirement blocks the merge (A3)."""
        if self.missing:
            raise RequirementNotEvaluated(
                f"cannot decide auto-merge: requirements never evaluated: {self.missing}"
            )
        if self.not_evaluated:
            return Verdict.UNVERIFIABLE
        if self.unsatisfied:
            return Verdict.FAIL
        return Verdict.PASS

    def may_merge(self) -> tuple[bool, list[str]]:
        """Section 21.2 in one call, including who is allowed to press the button."""
        blockers: list[str] = []
        if self.missing:
            blockers.append(f"requirements never evaluated: {self.missing}")
            return False, blockers
        verdict = self.verdict()
        if verdict is not Verdict.PASS:
            blockers.extend(f"requirement not satisfied: {name}" for name in self.unsatisfied)
            blockers.extend(f"requirement not evaluated: {name}" for name in self.not_evaluated)
        # A4: the implementing agent does not self-certify.
        if self.merge_actor is not None and self.merge_actor == self.implementing_agent_alias:
            blockers.append(
                f"merge actor {self.merge_actor!r} is the implementing agent; "
                "CI or an approved service identity performs the merge"
            )
        # A5: a PR CI never ran on is not green, it is unmeasured.
        if not self.ci_checks_present:
            blockers.append("no CI checks are present on the pull request")
        return (not blockers), blockers

    def as_evidence(self) -> dict[str, Any]:
        allowed, blockers = self.may_merge()
        return {
            "check": "requirement_evaluation_record",
            "expected": "all_thirteen_recorded",
            "pull_request": self.pull_request_ref,
            "candidate_commit": self.candidate_commit,
            "requirement_count": len(AUTO_MERGE_REQUIREMENTS),
            "recorded_count": len(self.records),
            "all_thirteen_recorded": self.all_thirteen_recorded,
            "requirements": [
                self.records[name].as_dict()
                for name in AUTO_MERGE_REQUIREMENTS
                if name in self.records
            ],
            "never_evaluated": self.missing,
            "merge_actor": self.merge_actor,
            "implementing_agent_alias": self.implementing_agent_alias,
            "ci_checks_present": self.ci_checks_present,
            "may_merge": allowed,
            "blockers": blockers,
            # Section 21.2: a green PR does not wait for a human message.
            "waits_for_human_message": False,
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.auto_merge_evaluation",
            created_by_alias="release-v04",
            body=self.as_evidence(),
        )
