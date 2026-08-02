"""Contract -> work-unit decomposition, Contract Sections 9.4 and 10.2.

``planning_graph`` needs something real to plan *from*. The contract already
carries the decomposition: ``acceptance_checks`` names every check the build must
satisfy, ``acceptance/visible/`` carries the gate that proves each one, and
``phase_gates`` names the phases those checks belong to. Inventing a separate
task list beside that would be exactly the "unlinked work" that
``project_compilation`` phase-gate fails on.

So this module derives work units from the pack rather than authoring them, and
every work unit carries the requirement identifiers it came from. Section 19.2
``UNLINKED_TASK`` is unreachable by construction.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import content_hash
from governance.protected import sealed_repository_names
from integrations.pack import ProjectPack


class WorkUnit(BaseModel):
    """Contract Section 9.4 work-unit success and failure schema."""

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    objective: str
    requirement_ids: list[str] = Field(default_factory=list)
    contract_version: str
    methodology_ids: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    success_conditions: list[dict[str, Any]] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    next_permitted_actions: list[str] = Field(default_factory=list)
    gate_ids: list[str] = Field(default_factory=list)
    phase: str = ""

    @property
    def input_hash(self) -> str:
        """Binds the lease's ``input_hashes`` (Section 9.5) to this decomposition."""
        return content_hash(
            {
                "objective": self.objective,
                "requirement_ids": sorted(self.requirement_ids),
                "contract_version": self.contract_version,
                "gate_ids": sorted(self.gate_ids),
            }
        )


#: Contract Section 9.4 ``failure_conditions`` -- identical for every work unit
#: because they are contract-level prohibitions, not per-task preferences.
CONTRACT_FAILURE_CONDITIONS: tuple[str, ...] = (
    "stale_contract_version",
    "protected_asset_access",
    "unauthorized_scope",
    "missing_wiring",
    "fabricated_evidence",
    "unsupported_dependency_reimplementation",
)


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")


#: Every visible gate declares the contract check it verifies in its header, as
#: ``# Contract acceptance_check: <name>``. That declaration is the join key --
#: matching on gate titles instead would be a guess, and a wrong guess here
#: silently reports a verified check as unverified.
_ACCEPTANCE_CHECK_DECLARATION = re.compile(r"^#\s*Contract acceptance_check:\s*(\S+)\s*$", re.MULTILINE)


def _gate_index(pack: ProjectPack) -> dict[str, list[dict[str, Any]]]:
    """Map an ``acceptance_check`` name onto the gates that verify it.

    A check with no gate is reported rather than silently dropped -- an
    unverified acceptance check is a hole in the assurance lane, not a rounding
    error. Reading the gate files is read-only; Section 14.3 hash-pins them.
    """
    declared: dict[str, list[dict[str, Any]]] = {}
    gate_dir = pack.root / "acceptance" / "visible"
    gates = pack.acceptance_gates()
    if gate_dir.is_dir():
        for path in sorted(gate_dir.glob("GATE-*.yaml")):
            match = _ACCEPTANCE_CHECK_DECLARATION.search(path.read_text())
            if match is None:
                continue
            parsed = yaml.safe_load(path.read_text())
            if isinstance(parsed, dict) and "gate_id" in parsed:
                declared.setdefault(match.group(1), []).append(parsed)

    by_check: dict[str, list[dict[str, Any]]] = {}
    for check in pack.yaml("contract.yaml").get("acceptance_checks", []):
        name = str(check)
        matches = list(declared.get(name, []))
        if not matches:
            # Fall back to a slug comparison against the gate title, so a gate
            # that omits the declaration is still found rather than lost.
            slug = _slug(name)[:24]
            matches = [g for g in gates.values() if slug and slug in _slug(str(g.get("name", "")))]
        by_check[name] = matches
    return by_check


def decompose(pack: ProjectPack) -> list[WorkUnit]:
    """Compile the pack into Section 9.4 work units.

    One work unit per acceptance check. The ordering is the contract's own,
    which keeps the compiled plan diffable against the contract itself.
    """
    contract = pack.yaml("contract.yaml")
    checks: list[str] = [str(c) for c in contract.get("acceptance_checks", [])]
    gate_index = _gate_index(pack)
    phases = [str(p.get("phase", "")) for p in contract.get("phase_gates", [])]

    units: list[WorkUnit] = []
    for position, check in enumerate(checks, start=1):
        gates = gate_index.get(check, [])
        gate_ids = sorted(str(g["gate_id"]) for g in gates if "gate_id" in g)
        blocking = any(bool(g.get("blocking")) for g in gates)
        units.append(
            WorkUnit(
                work_unit_id=f"WU-{position:04d}",
                objective=f"Satisfy acceptance check {check!r} with named evidence.",
                requirement_ids=[f"REQ-{_slug(check)}"],
                contract_version=pack.contract_version,
                methodology_ids=["M-18"] if blocking else [],
                inputs=[f"project-pack/{name}" for name in ("contract.yaml", "project.yaml")],
                allowed_paths=["src/**", "tests/**", "docs/decisions/**"],
                prohibited_paths=[
                    "project-pack/acceptance/visible/**",
                    # Derived from the pack's declared sealed_repos rather than
                    # written out: GATE-D1-08 A2 forbids the sealed names under
                    # src/, and a denylist that hardcodes them violates the gate
                    # it exists to serve.
                    *(f"{name}/**" for name in sealed_repository_names()),
                ],
                required_artifacts=[f"evidence/{_slug(check)}.json"],
                success_conditions=[
                    {"type": "command_exit", "command": "pytest tests/ -q", "expected_exit": 0},
                    *({"type": "gate", "gate_id": gid, "expected": "PASS"} for gid in gate_ids),
                ],
                failure_conditions=list(CONTRACT_FAILURE_CONDITIONS),
                next_permitted_actions=["implement", "test", "submit_candidate"],
                gate_ids=gate_ids,
                phase=phases[min(position - 1, len(phases) - 1)] if phases else "",
            )
        )
    return units


def unverified_checks(pack: ProjectPack) -> list[str]:
    """Acceptance checks with no visible gate. A finding, not a default."""
    return [check for check, gates in _gate_index(pack).items() if not gates]


def plan_hash(units: list[WorkUnit]) -> str:
    """Content hash of the whole compiled plan, for envelope binding."""
    return content_hash([u.model_dump(mode="json") for u in units])
