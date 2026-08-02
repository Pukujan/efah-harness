"""The gate runner.

Contract Sections 14.5, 17, 18, 21.1 and 25. Loads all 27 visible gates,
executes the assertions that have real checks behind them, emits the evidence
artifacts each gate names, and produces one enveloped result per gate.

Three rules the runner will not bend:

1. **No model judge in a deterministic verdict path.** Every gate is loaded
   through :func:`evaluation.gate_spec.load_gate`, which refuses a gate whose
   ``model_judge_in_verdict_path`` is anything but ``false``. A refused gate is
   reported as a load failure, never skipped. That is GATE-D2-20 A2.
2. **A gate cannot PASS without the evidence it named.** ``evidence_required``
   is a list of artifacts, not a list of intentions. A gate whose assertions
   all pass but whose evidence is missing is ``UNVERIFIABLE``.
3. **A partially-executable gate does not report PASS.** If some assertions
   have checks and others do not, the verdict is ``UNVERIFIABLE`` with the
   coverage recorded. Reporting PASS on the assertions that happened to be
   implemented is how a gate becomes decorative.

Run it with ``python -m evaluation.gate_runner``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from evaluation.binding import CandidateBinding
from evaluation.checks import (
    CHECKS,
    DEFAULT_NOT_EXECUTABLE_REASON,
    NOT_EXECUTABLE_REASONS,
    AssertionOutcome,
    AssertionStatus,
    GateContext,
)
from evaluation.evidence import EvidenceStore
from evaluation.gate_spec import (
    EXPECTED_GATE_COUNT,
    GATE_DIR,
    GateSpec,
    ModelJudgeInVerdictPath,
    assert_no_model_judge,
    load_gate,
)
from governance.envelope import CompiledObject, EvidenceTier
from governance.states import ProjectState, Verdict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Executability(StrEnum):
    """How much of a gate this runner can actually decide today."""

    EXECUTED = "EXECUTED"
    PARTIALLY_EXECUTABLE = "PARTIALLY_EXECUTABLE"
    NOT_YET_EXECUTABLE = "NOT_YET_EXECUTABLE"
    REFUSED_TO_LOAD = "REFUSED_TO_LOAD"


@dataclass
class AssertionResult:
    assertion_id: str
    claim: str
    method: str
    expected: str
    failure_state: str
    status: AssertionStatus
    findings: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.assertion_id,
            "claim": self.claim,
            "method": self.method,
            "expected": self.expected,
            "failure_state_if_violated": self.failure_state,
            "status": self.status.value,
            "findings": self.findings,
            "note": self.note,
        }


@dataclass
class GateResult:
    gate_id: str
    name: str
    day: int
    blocking: bool
    oracle_type: str
    candidate_commit: str
    contract_version: str
    source_hash: str
    executability: Executability
    verdict: Verdict
    assertions: list[AssertionResult] = field(default_factory=list)
    evidence_required: tuple[str, ...] = ()
    evidence_produced: list[dict[str, Any]] = field(default_factory=list)
    evidence_missing: list[str] = field(default_factory=list)
    on_fail_action: str = ""
    on_fail_state: str = ""
    remediation_must_not_include: str | None = None
    load_error: str | None = None
    #: Contract Section 17.3: this runner has no model in any verdict path.
    model_judge_in_verdict_path: bool = False
    evidence_tier: EvidenceTier = EvidenceTier.DETERMINISTIC_ORACLE

    @property
    def executed_count(self) -> int:
        return sum(1 for a in self.assertions if a.status is not AssertionStatus.NOT_IMPLEMENTED)

    @property
    def failed(self) -> list[AssertionResult]:
        return [a for a in self.assertions if a.status is AssertionStatus.FAIL]

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "name": self.name,
            "day": self.day,
            "blocking": self.blocking,
            "oracle_type": self.oracle_type,
            "model_judge_in_verdict_path": self.model_judge_in_verdict_path,
            "candidate_commit": self.candidate_commit,
            "contract_version": self.contract_version,
            "gate_source_hash": self.source_hash,
            "executability": self.executability.value,
            "verdict": self.verdict.value,
            "assertions_total": len(self.assertions),
            "assertions_executed": self.executed_count,
            "assertions": [a.as_dict() for a in self.assertions],
            "evidence_required": list(self.evidence_required),
            "evidence_produced": self.evidence_produced,
            "evidence_missing": self.evidence_missing,
            "on_fail": {
                "action": self.on_fail_action,
                "state": self.on_fail_state,
                "remediation_must_not_include": self.remediation_must_not_include,
            },
            "evidence_tier": self.evidence_tier.value,
            "load_error": self.load_error,
        }

    def to_compiled_object(self) -> CompiledObject:
        """Section 8: the result is a compiled object, envelope and all."""
        return CompiledObject.create(
            schema_id="efah.gate_result",
            created_by_alias="release-v04",
            body=self.as_dict(),
        )


@dataclass
class GateRunSummary:
    candidate_commit: str
    results: list[GateResult] = field(default_factory=list)
    gates_refused_to_load: dict[str, str] = field(default_factory=dict)
    no_judge_evidence: dict[str, Any] = field(default_factory=dict)

    def by_verdict(self, verdict: Verdict) -> list[GateResult]:
        return [r for r in self.results if r.verdict is verdict]

    def by_executability(self, value: Executability) -> list[GateResult]:
        return [r for r in self.results if r.executability is value]

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [r for r in self.results if r.blocking and r.verdict is Verdict.FAIL]

    def project_state(self) -> ProjectState:
        """What the run means for the project, in the contract's own vocabulary."""
        if self.gates_refused_to_load:
            return ProjectState.FAILED_ASSURANCE
        if self.blocking_failures:
            return ProjectState.FAILED_ASSURANCE
        if any(r.verdict is Verdict.UNVERIFIABLE for r in self.results):
            return ProjectState.RUNNING
        return ProjectState.VERIFIED_COMPLETE

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "efah.gate_run_summary",
            "candidate_commit": self.candidate_commit,
            "gates_loaded": len(self.results),
            "gates_expected": EXPECTED_GATE_COUNT,
            "gates_refused_to_load": self.gates_refused_to_load,
            "counts": {
                "PASS": len(self.by_verdict(Verdict.PASS)),
                "FAIL": len(self.by_verdict(Verdict.FAIL)),
                "UNVERIFIABLE": len(self.by_verdict(Verdict.UNVERIFIABLE)),
                "EXECUTED": len(self.by_executability(Executability.EXECUTED)),
                "PARTIALLY_EXECUTABLE": len(
                    self.by_executability(Executability.PARTIALLY_EXECUTABLE)
                ),
                "NOT_YET_EXECUTABLE": len(
                    self.by_executability(Executability.NOT_YET_EXECUTABLE)
                ),
            },
            "assertions_total": sum(len(r.assertions) for r in self.results),
            "assertions_executed": sum(r.executed_count for r in self.results),
            "project_state": self.project_state().value,
            "no_judge_in_verdict_path": self.no_judge_evidence,
            "gates": [r.as_dict() for r in self.results],
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.gate_run_summary",
            created_by_alias="release-v04",
            body=self.as_dict(),
        )


class GateRunner:
    """Executes gates. Owns no judgement of its own beyond the registered checks."""

    def __init__(
        self,
        binding: CandidateBinding | None = None,
        gate_dir: Path | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.binding = binding or CandidateBinding.from_head()
        self.gate_dir = gate_dir or GATE_DIR
        self.evidence = evidence_store or EvidenceStore(candidate_commit=self.binding.commit_sha)
        self._refused: dict[str, str] = {}
        self.gates = self._load_gates()
        self.context = GateContext(binding=self.binding, gates=self.gates, repo_root=REPO_ROOT)

    def _load_gates(self) -> dict[str, GateSpec]:
        """Load every gate. A refusal is recorded, never silently skipped."""
        gates: dict[str, GateSpec] = {}
        for path in sorted(self.gate_dir.glob("GATE-*.yaml")):
            try:
                spec = load_gate(path)
            except ModelJudgeInVerdictPath as exc:
                self._refused[path.stem] = str(exc)
                continue
            gates[spec.gate_id] = spec
        return gates

    def run_gate(self, gate: GateSpec) -> GateResult:
        results: list[AssertionResult] = []
        for assertion in gate.assertions:
            key = (gate.gate_id, assertion.assertion_id)
            check = CHECKS.get(key)
            if check is None:
                results.append(
                    AssertionResult(
                        assertion_id=assertion.assertion_id,
                        claim=assertion.claim,
                        method=assertion.method,
                        expected=assertion.expected,
                        failure_state=assertion.failure_state,
                        status=AssertionStatus.NOT_IMPLEMENTED,
                        note=NOT_EXECUTABLE_REASONS.get(key, DEFAULT_NOT_EXECUTABLE_REASON),
                    )
                )
                continue
            try:
                outcome: AssertionOutcome = check(self.context, gate, assertion)
            except Exception as exc:
                results.append(
                    AssertionResult(
                        assertion_id=assertion.assertion_id,
                        claim=assertion.claim,
                        method=assertion.method,
                        expected=assertion.expected,
                        failure_state=assertion.failure_state,
                        status=AssertionStatus.FAIL,
                        findings=[f"check raised {type(exc).__name__}: {exc}"],
                    )
                )
                continue
            for name, payload in outcome.evidence.items():
                self.evidence.add(gate.gate_id, name, payload)
            results.append(
                AssertionResult(
                    assertion_id=assertion.assertion_id,
                    claim=assertion.claim,
                    method=assertion.method,
                    expected=assertion.expected,
                    failure_state=assertion.failure_state,
                    status=outcome.status,
                    findings=outcome.findings,
                    note=outcome.note,
                )
            )

        executed = [r for r in results if r.status is not AssertionStatus.NOT_IMPLEMENTED]
        if not executed:
            executability = Executability.NOT_YET_EXECUTABLE
        elif len(executed) < len(results):
            executability = Executability.PARTIALLY_EXECUTABLE
        else:
            executability = Executability.EXECUTED

        missing_evidence = self.evidence.missing_for(gate.gate_id, gate.evidence_required)

        if any(r.status is AssertionStatus.FAIL for r in results):
            verdict = Verdict.FAIL
        elif executability is Executability.NOT_YET_EXECUTABLE or executability is Executability.PARTIALLY_EXECUTABLE or any(r.status is AssertionStatus.UNVERIFIABLE for r in results):
            verdict = Verdict.UNVERIFIABLE
        elif missing_evidence:
            # Section 18: "done" without named evidence is invalid.
            verdict = Verdict.UNVERIFIABLE
        else:
            verdict = Verdict.PASS

        return GateResult(
            gate_id=gate.gate_id,
            name=gate.name,
            day=gate.day,
            blocking=gate.blocking,
            oracle_type=gate.oracle_type,
            candidate_commit=self.binding.commit_sha,
            contract_version=gate.contract_version,
            source_hash=gate.source_hash,
            executability=executability,
            verdict=verdict,
            assertions=results,
            evidence_required=gate.evidence_required,
            evidence_produced=self.evidence.references_for(gate.gate_id),
            evidence_missing=missing_evidence,
            on_fail_action=gate.on_fail_action,
            on_fail_state=gate.on_fail_state,
            remediation_must_not_include=gate.remediation_must_not_include,
        )

    def run(self, gate_ids: list[str] | None = None) -> GateRunSummary:
        summary = GateRunSummary(candidate_commit=self.binding.commit_sha)
        summary.gates_refused_to_load = dict(self._refused)
        summary.no_judge_evidence = assert_no_model_judge(self.gates)
        selected = gate_ids or sorted(self.gates)
        for gate_id in selected:
            summary.results.append(self.run_gate(self.gates[gate_id]))
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the visible acceptance gates.")
    parser.add_argument("--gate", action="append", dest="gates", help="run only this gate id")
    parser.add_argument("--json", dest="json_out", help="write the full summary here")
    parser.add_argument(
        "--write-evidence",
        action="store_true",
        help="persist evidence artifacts under evidence/gates/",
    )
    args = parser.parse_args(argv)

    runner = GateRunner()
    summary = runner.run(args.gates)

    print(f"candidate commit: {summary.candidate_commit}")
    print(f"gates loaded: {len(summary.results)} of {EXPECTED_GATE_COUNT} expected")
    if summary.gates_refused_to_load:
        print("gates REFUSED to load (model judge declared in the verdict path):")
        for name, reason in summary.gates_refused_to_load.items():
            print(f"  {name}: {reason}")
    print()
    for result in summary.results:
        print(
            f"  {result.gate_id:<12} {result.verdict.value:<13} {result.executability.value:<22}"
            f" {result.executed_count}/{len(result.assertions)} assertions"
        )
        for failure in result.failed:
            print(f"        FAIL {failure.assertion_id}: {failure.claim}")
            for finding in failure.findings[:5]:
                print(f"             {finding}")
    counts = summary.as_dict()["counts"]
    print()
    print(
        f"PASS={counts['PASS']} FAIL={counts['FAIL']} UNVERIFIABLE={counts['UNVERIFIABLE']}"
        f"  (executed={counts['EXECUTED']}, partial={counts['PARTIALLY_EXECUTABLE']},"
        f" not-yet-executable={counts['NOT_YET_EXECUTABLE']})"
    )
    print(f"project state: {summary.project_state().value}")

    if args.write_evidence:
        written = runner.evidence.write()
        print(f"wrote {len(written)} evidence artifacts under evidence/gates/")
    if args.json_out:
        obj = summary.to_compiled_object()
        Path(args.json_out).write_text(
            json.dumps(obj.model_dump(mode="json"), indent=2, sort_keys=True, default=str) + "\n"
        )
        print(f"wrote {args.json_out}")

    return 1 if summary.blocking_failures or summary.gates_refused_to_load else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
