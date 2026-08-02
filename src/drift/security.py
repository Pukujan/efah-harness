"""Security-review scope boundary.

Contract Section 19.5, in full:

    A security finding blocks only when it:
    - maps to an approved requirement, threat, risk, or policy;
    - provides concrete evidence or an executable exploit/probe;
    - states the smallest compliant remediation.
    Other findings become ``OUT_OF_SCOPE_OBSERVATION`` and do not expand the
    build.

Section 26 names the failure this exists to prevent: "Security agents expand
scope -> frozen threat model, requirement-linked findings, out-of-scope
observation state." GATE-D2-21 A4 and A5 are the executable form.

Three properties this module guarantees, each with a test:

1. all three conditions are required -- two out of three is an observation;
2. an observation carries no remediation work: :func:`admit` returns it in a
   separate list and :func:`detect_expansion` fails the build if a requirement
   or task appeared that traces to one;
3. the approved-reference set is supplied by the compiled contract, never by the
   finding. A finding cannot mint its own authority by naming a requirement that
   does not exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from governance.states import DriftFinding

#: Section 19.5's three conditions, named so a failure says which one failed.
CONDITION_MAPPED = "maps_to_approved_requirement_threat_risk_or_policy"
CONDITION_EVIDENCE = "has_concrete_evidence_or_executable_probe"
CONDITION_REMEDIATION = "states_the_smallest_compliant_remediation"
REQUIRED_CONDITIONS = (CONDITION_MAPPED, CONDITION_EVIDENCE, CONDITION_REMEDIATION)


@dataclass(frozen=True)
class SecurityFinding:
    """A candidate security finding, as submitted by any role."""

    finding_id: str
    title: str
    #: requirement / threat / risk / policy identifiers the finding claims
    mapped_refs: tuple[str, ...] = ()
    #: concrete evidence pointers (artifact hashes, log locations, traces)
    evidence: tuple[str, ...] = ()
    #: a command or test that demonstrates the issue
    executable_probe: str | None = None
    #: the smallest change that brings the system back into compliance
    smallest_remediation: str | None = None
    severity: str = "unknown"
    proposed_requirements: tuple[str, ...] = ()
    proposed_tasks: tuple[str, ...] = ()

    def as_body(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "title": self.title,
            "mapped_refs": list(self.mapped_refs),
            "evidence": list(self.evidence),
            "executable_probe": self.executable_probe,
            "smallest_remediation": self.smallest_remediation,
            "severity": self.severity,
        }


@dataclass
class SecurityClassification:
    finding_id: str
    blocks: bool
    finding_type: str | None
    satisfied: tuple[str, ...] = ()
    unsatisfied: tuple[str, ...] = ()
    admitted_refs: tuple[str, ...] = ()
    rationale: str = ""

    def as_body(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "blocks": self.blocks,
            "finding_type": self.finding_type,
            "satisfied_conditions": list(self.satisfied),
            "unsatisfied_conditions": list(self.unsatisfied),
            "admitted_refs": list(self.admitted_refs),
            "rationale": self.rationale,
            "contract_ref": "contract.md#19.5",
        }


def classify(finding: SecurityFinding, approved_refs: Iterable[str]) -> SecurityClassification:
    """Apply Section 19.5's three conditions. All three, or it does not block."""
    approved = set(approved_refs)
    admitted = tuple(sorted(ref for ref in finding.mapped_refs if ref in approved))

    satisfied: list[str] = []
    unsatisfied: list[str] = []
    (satisfied if admitted else unsatisfied).append(CONDITION_MAPPED)
    has_evidence = bool(finding.evidence) or bool(finding.executable_probe)
    (satisfied if has_evidence else unsatisfied).append(CONDITION_EVIDENCE)
    has_remediation = bool(finding.smallest_remediation and finding.smallest_remediation.strip())
    (satisfied if has_remediation else unsatisfied).append(CONDITION_REMEDIATION)

    if not unsatisfied:
        return SecurityClassification(
            finding_id=finding.finding_id,
            blocks=True,
            finding_type=None,
            satisfied=tuple(satisfied),
            admitted_refs=admitted,
            rationale=(
                "in scope: mapped to "
                + ", ".join(admitted)
                + " with evidence and a stated smallest remediation"
            ),
        )

    unmapped_refs = tuple(sorted(set(finding.mapped_refs) - approved))
    rationale = f"out of scope: {', '.join(unsatisfied)} not satisfied"
    if unmapped_refs:
        rationale += f"; references not in the approved set: {list(unmapped_refs)}"
    return SecurityClassification(
        finding_id=finding.finding_id,
        blocks=False,
        finding_type=str(DriftFinding.OUT_OF_SCOPE_OBSERVATION),
        satisfied=tuple(satisfied),
        unsatisfied=tuple(unsatisfied),
        admitted_refs=admitted,
        rationale=rationale,
    )


def blocking_schema_violations(finding: SecurityFinding) -> list[str]:
    """GATE-D2-21 A5: what a blocking finding is missing, if anything."""
    missing: list[str] = []
    if not (finding.evidence or finding.executable_probe):
        missing.append(CONDITION_EVIDENCE)
    if not (finding.smallest_remediation and finding.smallest_remediation.strip()):
        missing.append(CONDITION_REMEDIATION)
    if not finding.mapped_refs:
        missing.append(CONDITION_MAPPED)
    return missing


@dataclass
class SecurityScopeReport:
    blocking: list[SecurityClassification] = field(default_factory=list)
    observations: list[SecurityClassification] = field(default_factory=list)
    expansions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def expands_the_build(self) -> bool:
        return bool(self.expansions)

    def as_body(self) -> dict[str, Any]:
        return {
            "blocking_count": len(self.blocking),
            "observation_count": len(self.observations),
            "blocking": [c.as_body() for c in self.blocking],
            "observations": [c.as_body() for c in self.observations],
            "expansions": self.expansions,
            "expands_the_build": self.expands_the_build,
            "contract_ref": "contract.md#19.5,#26",
        }


def admit(findings: Sequence[SecurityFinding], approved_refs: Iterable[str]) -> SecurityScopeReport:
    """Split findings into blocking and observation, and reject expansion.

    An observation that proposes new requirements or tasks is not merely
    ignored: proposing them *is* the scope expansion Section 26 forbids, so it
    is recorded as ``OUT_OF_SCOPE_SECURITY_EXPANSION``.
    """
    approved = set(approved_refs)
    report = SecurityScopeReport()
    for finding in findings:
        classification = classify(finding, approved)
        if classification.blocks:
            missing = blocking_schema_violations(finding)
            if missing:  # defence in depth; classify() already required these
                classification.blocks = False
                classification.finding_type = str(DriftFinding.OUT_OF_SCOPE_OBSERVATION)
                classification.unsatisfied = tuple(missing)
                report.observations.append(classification)
                continue
            report.blocking.append(classification)
            continue

        report.observations.append(classification)
        if finding.proposed_requirements or finding.proposed_tasks:
            report.expansions.append(
                {
                    "finding": str(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION),
                    "finding_id": finding.finding_id,
                    "proposed_requirements": list(finding.proposed_requirements),
                    "proposed_tasks": list(finding.proposed_tasks),
                    "detail": (
                        "an out-of-scope security observation proposed new work; "
                        "Section 19.5 says it does not expand the build"
                    ),
                    "contract_ref": "contract.md#19.5,#26",
                }
            )
    return report


def detect_expansion(
    requirement_ids_before: Iterable[str],
    requirement_ids_after: Iterable[str],
    observations: Sequence[SecurityClassification],
) -> list[dict[str, Any]]:
    """Fail if a requirement appeared that traces to an out-of-scope finding."""
    before = set(requirement_ids_before)
    added = sorted(set(requirement_ids_after) - before)
    if not added or not observations:
        return []
    return [
        {
            "finding": str(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION),
            "added_requirement_ids": added,
            "out_of_scope_finding_ids": [o.finding_id for o in observations],
            "detail": "requirements were added while out-of-scope security observations were open",
            "contract_ref": "contract.md#19.5",
        }
    ]
