"""Requirement extraction from the approved contract.

Contract Section 8 first output: "requirement IDs and acceptance criteria".

A requirement here is never invented by the compiler. Every one carries a
``source`` pointer back into the pack, so Section 1.2's authority order is
checkable: priority 2 (compiled requirements) is derivable from priority 1 (the
contract) by re-running this module.

Requirement families, all mechanically derived:

===============  ======================================================
``REQ-AC-nnn``   ``contract.yaml -> acceptance_checks``. Each MUST resolve
                 to a gate file through ``acceptance/visible/INDEX.yaml``.
                 A check with no gate is a finding (Section 27: "done"
                 without named evidence is invalid).
``REQ-PH-nnn``   ``contract.yaml -> phase_gates`` (Section 8.1 matrix).
``REQ-CC-nnn``   ``contract.yaml -> contract_compiler_outputs`` (Section 8).
``REQ-AM-nnn``   ``contract.yaml -> auto_merge_requirements`` (Section 21.2).
``REQ-NG-nnn``   ``contract.yaml -> product.non_goals`` (Section 28).
``REQ-DR-nnn``   ``contract.yaml -> scope_drift.finding_types`` (Section 19.2).
===============  ======================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from governance.states import ProjectState
from integrations.pack import ProjectPack

INDEX_RELATIVE_PATH = "acceptance/visible/INDEX.yaml"


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One machine-checkable criterion attached to a requirement."""

    criterion_id: str
    requirement_id: str
    claim: str
    method: str
    expected: str
    failure_state: str
    gate_id: str | None = None
    source: str = ""

    def as_body(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "requirement_id": self.requirement_id,
            "claim": self.claim,
            "method": self.method,
            "expected": self.expected,
            "failure_state": self.failure_state,
            "gate_id": self.gate_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    kind: str
    statement: str
    source: str
    risk_class: str
    blocking: bool
    gate_ids: tuple[str, ...] = ()
    gate_files: tuple[str, ...] = ()
    day: int | None = None
    criteria: tuple[AcceptanceCriterion, ...] = ()
    contract_refs: tuple[str, ...] = ()

    def as_body(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "statement": self.statement,
            "source": self.source,
            "risk_class": self.risk_class,
            "blocking": self.blocking,
            "gate_ids": list(self.gate_ids),
            "gate_files": list(self.gate_files),
            "day": self.day,
            "acceptance_criterion_ids": [c.criterion_id for c in self.criteria],
            "contract_refs": list(self.contract_refs),
        }


@dataclass(frozen=True)
class CompilationFinding:
    """A defect found while compiling.

    ``failure_state`` is drawn from :class:`governance.states.ProjectState` --
    the compiler never invents a state string. ``severity`` distinguishes a
    finding that fails compilation from one the orchestrator must merely know.
    """

    kind: str
    detail: str
    severity: str  # "blocking" | "observation"
    failure_state: ProjectState | None = None
    subject: str = ""

    def as_body(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "severity": self.severity,
            "failure_state": str(self.failure_state) if self.failure_state else None,
            "subject": self.subject,
        }


@dataclass
class RequirementCatalog:
    requirements: list[Requirement] = field(default_factory=list)
    criteria: list[AcceptanceCriterion] = field(default_factory=list)
    findings: list[CompilationFinding] = field(default_factory=list)
    #: acceptance_check name -> requirement id
    by_check: dict[str, str] = field(default_factory=dict)
    #: gate_id -> requirement id
    by_gate: dict[str, str] = field(default_factory=dict)

    def get(self, requirement_id: str) -> Requirement:
        for req in self.requirements:
            if req.requirement_id == requirement_id:
                return req
        raise KeyError(requirement_id)

    def ids_of_kind(self, kind: str) -> list[str]:
        return [r.requirement_id for r in self.requirements if r.kind == kind]


def load_acceptance_index(root: Path) -> dict[str, Any]:
    path = root / INDEX_RELATIVE_PATH
    if not path.is_file():
        return {}
    parsed = yaml.safe_load(path.read_text())
    return parsed if isinstance(parsed, dict) else {}


def build_catalog(pack: ProjectPack) -> RequirementCatalog:
    """Compile every requirement family. Pure function of the pack."""
    catalog = RequirementCatalog()
    contract = pack.yaml("contract.yaml")
    gates = pack.acceptance_gates()
    index = load_acceptance_index(pack.root)

    _acceptance_checks(catalog, contract, gates, index)
    _phase_requirements(catalog, contract)
    _compiler_output_requirements(catalog, contract)
    _auto_merge_requirements(catalog, contract)
    _non_goal_requirements(catalog, contract)
    _drift_requirements(catalog, contract)
    return catalog


# --------------------------------------------------------------------------
# REQ-AC: acceptance checks, each bound to its gate file
# --------------------------------------------------------------------------


def _acceptance_checks(
    catalog: RequirementCatalog,
    contract: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    index: dict[str, Any],
) -> None:
    coverage = {row["check"]: row for row in index.get("coverage", []) if isinstance(row, dict)}
    checks = contract.get("acceptance_checks", [])
    for position, check in enumerate(checks, start=1):
        row = coverage.get(check)
        req_id = f"REQ-AC-{position:03d}"
        source = f"contract.yaml#acceptance_checks[{position - 1}]"

        if row is None:
            # Section 27: an acceptance check with no evidence path is invalid.
            catalog.findings.append(
                CompilationFinding(
                    kind="ACCEPTANCE_CHECK_WITHOUT_GATE",
                    detail=(
                        f"acceptance_check {check!r} has no entry in {INDEX_RELATIVE_PATH}; "
                        "there is no named evidence path for it"
                    ),
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=check,
                )
            )
            continue

        gate_file = row.get("gate", "")
        gate_id = _gate_id_for_file(gates, gate_file)
        if gate_id is None:
            catalog.findings.append(
                CompilationFinding(
                    kind="ACCEPTANCE_CHECK_WITHOUT_GATE",
                    detail=(
                        f"acceptance_check {check!r} points at gate file {gate_file!r} "
                        "which does not exist or declares no gate_id"
                    ),
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=check,
                )
            )
            continue

        gate = gates[gate_id]
        criteria: list[AcceptanceCriterion] = []
        for assertion in gate.get("assertions", []):
            criterion = AcceptanceCriterion(
                criterion_id=f"{req_id}-{assertion['id']}",
                requirement_id=req_id,
                claim=str(assertion.get("claim", "")).strip(),
                method=str(assertion.get("method", "")).strip(),
                expected=str(assertion.get("expected", "")).strip(),
                failure_state=str(assertion.get("failure_state", "")).strip(),
                gate_id=gate_id,
                source=f"{gate_file}#assertions.{assertion['id']}",
            )
            criteria.append(criterion)
        catalog.criteria.extend(criteria)

        blocking = bool(gate.get("blocking", False))
        requirement = Requirement(
            requirement_id=req_id,
            kind="acceptance_check",
            statement=str(gate.get("intent", gate.get("name", check))).strip(),
            source=source,
            risk_class="high" if blocking else "medium",
            blocking=blocking,
            gate_ids=(gate_id,),
            gate_files=(gate_file,),
            day=row.get("day") or gate.get("day"),
            criteria=tuple(criteria),
            contract_refs=(check, str(row.get("contract_ref", "")) or "contract.yaml#acceptance_checks"),
        )
        catalog.requirements.append(requirement)
        catalog.by_check[check] = req_id
        catalog.by_gate[gate_id] = req_id

    # A gate that no acceptance check claims is also a defect: it is an
    # obligation nothing obliges the builder to satisfy (the exact failure
    # AMENDMENT-001 corrected for vendor_neutral_after_deadline).
    for gate_id in sorted(gates):
        if gate_id not in catalog.by_gate:
            catalog.findings.append(
                CompilationFinding(
                    kind="GATE_WITHOUT_ACCEPTANCE_CHECK",
                    detail=f"gate {gate_id} is defined but no acceptance_check requires it",
                    severity="blocking",
                    failure_state=ProjectState.FAILED_CONTRACT,
                    subject=gate_id,
                )
            )


def _gate_id_for_file(gates: dict[str, dict[str, Any]], gate_file: str) -> str | None:
    if not gate_file:
        return None
    stem = gate_file.split("/")[-1]
    for gate_id in gates:
        if stem.startswith(gate_id):
            return gate_id
    return None


# --------------------------------------------------------------------------
# The remaining families
# --------------------------------------------------------------------------


def _phase_requirements(catalog: RequirementCatalog, contract: dict[str, Any]) -> None:
    for position, phase in enumerate(contract.get("phase_gates", []), start=1):
        req_id = f"REQ-PH-{position:03d}"
        name = phase["phase"]
        criterion = AcceptanceCriterion(
            criterion_id=f"{req_id}-A1",
            requirement_id=req_id,
            claim=f"Phase {name} reaches its pass condition.",
            method="phase_gate_evaluation",
            expected=str(phase.get("pass", "")).strip(),
            failure_state=str(ProjectState.FAILED_CONTRACT),
            source=f"contract.yaml#phase_gates[{position - 1}]",
        )
        catalog.criteria.append(criterion)
        catalog.requirements.append(
            Requirement(
                requirement_id=req_id,
                kind="phase",
                statement=f"Phase {name}: {phase.get('pass', '')}",
                source=f"contract.yaml#phase_gates[{position - 1}]",
                risk_class="high",
                blocking=True,
                criteria=(criterion,),
                contract_refs=(name, "contract.md#8.1"),
            )
        )


def _compiler_output_requirements(catalog: RequirementCatalog, contract: dict[str, Any]) -> None:
    for position, output in enumerate(contract.get("contract_compiler_outputs", []), start=1):
        req_id = f"REQ-CC-{position:03d}"
        criterion = AcceptanceCriterion(
            criterion_id=f"{req_id}-A1",
            requirement_id=req_id,
            claim=f"The contract compiler emits {output}.",
            method="output_manifest_check",
            expected="present_and_non_empty",
            failure_state=str(ProjectState.FAILED_CONTRACT),
            gate_id="GATE-D1-03",
            source=f"contract.yaml#contract_compiler_outputs[{position - 1}]",
        )
        catalog.criteria.append(criterion)
        catalog.requirements.append(
            Requirement(
                requirement_id=req_id,
                kind="compiler_output",
                statement=f"The contract compiler MUST produce output {output}.",
                source=f"contract.yaml#contract_compiler_outputs[{position - 1}]",
                risk_class="high",
                blocking=True,
                gate_ids=("GATE-D1-03",),
                criteria=(criterion,),
                contract_refs=(output, "contract.md#8"),
            )
        )


def _auto_merge_requirements(catalog: RequirementCatalog, contract: dict[str, Any]) -> None:
    for position, (name, expected) in enumerate(sorted(contract.get("auto_merge_requirements", {}).items()), start=1):
        req_id = f"REQ-AM-{position:03d}"
        criterion = AcceptanceCriterion(
            criterion_id=f"{req_id}-A1",
            requirement_id=req_id,
            claim=f"Auto-merge condition {name} holds.",
            method="auto_merge_condition_check",
            expected=str(expected),
            failure_state=str(ProjectState.FAILED_ASSURANCE),
            gate_id="GATE-D3-25",
            source=f"contract.yaml#auto_merge_requirements.{name}",
        )
        catalog.criteria.append(criterion)
        catalog.requirements.append(
            Requirement(
                requirement_id=req_id,
                kind="auto_merge",
                statement=f"Auto-merge requires {name} == {expected}.",
                source=f"contract.yaml#auto_merge_requirements.{name}",
                risk_class="high",
                blocking=True,
                gate_ids=("GATE-D3-25",),
                criteria=(criterion,),
                contract_refs=(name, "contract.md#21.2"),
            )
        )


def _non_goal_requirements(catalog: RequirementCatalog, contract: dict[str, Any]) -> None:
    for position, goal in enumerate(contract.get("product", {}).get("non_goals", []), start=1):
        req_id = f"REQ-NG-{position:03d}"
        criterion = AcceptanceCriterion(
            criterion_id=f"{req_id}-A1",
            requirement_id=req_id,
            claim=f"The build does not deliver the non-goal {goal}.",
            method="static_ast_type_policy",
            expected="absent_from_delivered_runtime",
            failure_state=str(ProjectState.FAILED_CONTRACT),
            source=f"contract.yaml#product.non_goals[{position - 1}]",
        )
        catalog.criteria.append(criterion)
        catalog.requirements.append(
            Requirement(
                requirement_id=req_id,
                kind="non_goal",
                statement=f"MUST NOT build {goal} (contract non-goal).",
                source=f"contract.yaml#product.non_goals[{position - 1}]",
                risk_class="high",
                blocking=True,
                criteria=(criterion,),
                contract_refs=(goal, "contract.md#28"),
            )
        )


def _drift_requirements(catalog: RequirementCatalog, contract: dict[str, Any]) -> None:
    for position, finding in enumerate(contract.get("scope_drift", {}).get("finding_types", []), start=1):
        req_id = f"REQ-DR-{position:03d}"
        criterion = AcceptanceCriterion(
            criterion_id=f"{req_id}-A1",
            requirement_id=req_id,
            claim=f"The drift engine detects {finding}.",
            method="negative_control_probe",
            expected="detected",
            failure_state=str(ProjectState.FAILED_ASSURANCE),
            gate_id="GATE-D2-21",
            source=f"contract.yaml#scope_drift.finding_types[{position - 1}]",
        )
        catalog.criteria.append(criterion)
        catalog.requirements.append(
            Requirement(
                requirement_id=req_id,
                kind="drift_finding",
                statement=f"The drift engine MUST emit {finding} when the condition holds.",
                source=f"contract.yaml#scope_drift.finding_types[{position - 1}]",
                risk_class="high",
                blocking=True,
                gate_ids=("GATE-D2-21",),
                criteria=(criterion,),
                contract_refs=(finding, "contract.md#19.2"),
            )
        )
