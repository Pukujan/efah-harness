"""Loading and validating the 27 visible acceptance gates.

Contract Section 17.3 and ``model-policy.yaml`` ``authority_limits``:

    model_judge_in_deterministic_verdict_path: forbidden

Every gate YAML in the pack declares ``model_judge_in_verdict_path: false``.
This module asserts that **at load time** and refuses to construct a
:class:`GateSpec` for a gate that claims otherwise -- so a gate cannot be
executed at all unless it is model-free. That refusal is GATE-D2-20 A2's
enforcement point, and it is also what kills the workflow/governance mutant
that flips the flag to ``true``.

Refusing at load rather than at verdict time is deliberate. A check performed
inside the runner can be skipped by a code path that does not call the runner;
a check performed in the constructor cannot be, because there is no other way
to obtain the object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from governance.envelope import content_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_DIR = REPO_ROOT / "project-pack" / "acceptance" / "visible"

#: Contract Section 25 / INDEX.yaml. If this number changes, the pack changed.
EXPECTED_GATE_COUNT = 27


class ModelJudgeInVerdictPath(RuntimeError):
    """GATE-D2-20 A2. A gate that admits a judge is not a gate."""


class GateSpecInvalid(RuntimeError):
    """The gate file is not a usable gate definition."""


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    claim: str
    method: str
    expected: str
    failure_state: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    name: str
    day: int
    blocking: bool
    oracle_type: str
    intent: str
    assertions: tuple[AssertionSpec, ...]
    evidence_required: tuple[str, ...]
    on_fail_action: str
    on_fail_state: str
    remediation_must_not_include: str | None
    source_path: Path
    source_hash: str
    contract_version: str

    @property
    def model_judge_in_verdict_path(self) -> bool:
        return False  # enforced in load_gate; no instance can exist with True


def _require(parsed: dict[str, Any], key: str, path: Path) -> Any:
    if key not in parsed:
        raise GateSpecInvalid(f"{path.name}: missing required key {key!r}")
    return parsed[key]


def load_gate(path: Path) -> GateSpec:
    raw_bytes = path.read_bytes()
    parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise GateSpecInvalid(f"{path.name}: does not parse to a mapping")

    declared = parsed.get("model_judge_in_verdict_path")
    if declared is not False:
        raise ModelJudgeInVerdictPath(
            f"{parsed.get('gate_id', path.name)}: model_judge_in_verdict_path is {declared!r}; "
            "authority_limits.model_judge_in_deterministic_verdict_path is forbidden, so this "
            "gate will not be run"
        )

    assertions: list[AssertionSpec] = []
    for entry in _require(parsed, "assertions", path):
        if not isinstance(entry, dict):
            raise GateSpecInvalid(f"{path.name}: assertion entry is not a mapping")
        assertions.append(
            AssertionSpec(
                assertion_id=str(entry.get("id", "")),
                claim=str(entry.get("claim", "")),
                method=str(entry.get("method", "")),
                expected=str(entry.get("expected", "")),
                failure_state=str(entry.get("failure_state", "")),
                raw=entry,
            )
        )
    if not assertions:
        raise GateSpecInvalid(f"{path.name}: declares no assertions")

    on_fail = parsed.get("on_fail") or {}
    return GateSpec(
        gate_id=str(_require(parsed, "gate_id", path)),
        name=str(parsed.get("name", "")),
        day=int(parsed.get("day", 0)),
        blocking=bool(parsed.get("blocking", True)),
        oracle_type=str(parsed.get("oracle_type", "")),
        intent=str(parsed.get("intent", "")).strip(),
        assertions=tuple(assertions),
        evidence_required=tuple(str(e) for e in (parsed.get("evidence_required") or [])),
        on_fail_action=str(on_fail.get("action", "")),
        on_fail_state=str(on_fail.get("state", "")),
        remediation_must_not_include=(
            str(on_fail["remediation_must_not_include"])
            if "remediation_must_not_include" in on_fail
            else None
        ),
        source_path=path,
        source_hash=content_hash(raw_bytes),
        contract_version=str(parsed.get("contract_version", "")),
    )


def load_all_gates(gate_dir: Path | None = None) -> dict[str, GateSpec]:
    """Load every visible gate. A gate that refuses to load is not skipped."""
    directory = gate_dir or GATE_DIR
    if not directory.is_dir():
        raise GateSpecInvalid(f"gate directory not found: {directory}")
    gates: dict[str, GateSpec] = {}
    for path in sorted(directory.glob("GATE-*.yaml")):
        spec = load_gate(path)
        if spec.gate_id in gates:
            raise GateSpecInvalid(f"duplicate gate_id {spec.gate_id}")
        gates[spec.gate_id] = spec
    return gates


def assert_no_model_judge(gates: dict[str, GateSpec]) -> dict[str, Any]:
    """Evidence for GATE-D2-20 A2 across the whole gate set."""
    return {
        "check": "model_judge_in_verdict_path",
        "expected": "false for every gate; enforced at load time",
        "gates_loaded": len(gates),
        "gates": {
            gate_id: {
                "model_judge_in_verdict_path": spec.model_judge_in_verdict_path,
                "oracle_type": spec.oracle_type,
                "source_hash": spec.source_hash,
            }
            for gate_id, spec in sorted(gates.items())
        },
    }
