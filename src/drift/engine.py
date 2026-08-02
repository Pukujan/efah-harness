"""Continuous scope-drift engine.

Contract Section 19.1 sets the comparison, and it is a chain, not a pair::

    approved contract
    vs compiled requirements
    vs project plan
    vs active tasks
    vs changed artifacts
    vs test/evaluation claims
    vs release contents

Section 19.2 fixes the finding vocabulary; this module emits only
:class:`governance.states.DriftFinding` members. Section 19.5's security
boundary lives in :mod:`drift.security` and is applied here so that an
out-of-scope security finding cannot enter the blocking set by another door.

Acceptance gate: ``GATE-D2-21-scope-drift-and-security-expansion-blocked``.
Every assertion in it has a negative control in ``tests/unit/test_drift.py`` --
a detector that cannot be made to fire is not evidence that nothing is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Sequence

from contracts.compiler import CompiledProject
from drift import security
from governance.envelope import CONTRACT_VERSION
from governance.states import DriftFinding, ProjectState, TaskState

#: Section 5.2 wiring manifest. All nine or the module is not complete.
WIRING_FIELDS = (
    "provides",
    "consumes",
    "startup_registration",
    "configuration_schema",
    "health_check",
    "integration_test",
    "e2e_path",
    "telemetry_span",
    "dashboard_projection",
)

#: Task states at which a wiring claim becomes checkable (Section 5.2).
COMPLETION_CLAIM_STATES = frozenset(
    {TaskState.CANDIDATE_COMPLETE, TaskState.VERIFYING, TaskState.PASSED, TaskState.MERGED}
)


@dataclass(frozen=True)
class Finding:
    """One typed drift finding. ``finding`` is always a ``DriftFinding``."""

    finding: str
    subject: str
    detail: str
    blocks: bool = True
    contract_ref: str = "contract.md#19.2"
    evidence: tuple[str, ...] = ()

    def as_body(self) -> dict[str, Any]:
        return {
            "finding": self.finding,
            "subject": self.subject,
            "detail": self.detail,
            "blocks": self.blocks,
            "contract_ref": self.contract_ref,
            "evidence": list(self.evidence),
        }


@dataclass
class ActiveTask:
    """A task as the ledger currently reports it."""

    task_id: str
    title: str = ""
    requirement_ids: tuple[str, ...] = ()
    contract_version: str = CONTRACT_VERSION
    state: str = str(TaskState.RUNNING)
    changed_paths: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    prohibited_paths: tuple[str, ...] = ()
    role_assignments: dict[str, str] = field(default_factory=dict)
    wiring_manifest: dict[str, Any] = field(default_factory=dict)
    introduces_components: tuple[str, ...] = ()
    build_vs_integrate_record: dict[str, Any] | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class ArtifactClaim:
    artifact_id: str
    content_hash: str = ""
    produced_by_task: str = ""
    contract_version: str = CONTRACT_VERSION
    source_input_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class EvaluationClaim:
    """A claimed gate result, with the assertion hash it ran against."""

    gate_id: str
    verdict: str = "PASS"
    assertion_hash: str = ""
    oracle_version: str = ""
    model_judge_in_verdict_path: bool = False
    evidence: tuple[str, ...] = ()


@dataclass
class ReleaseContents:
    commit: str = ""
    task_ids: tuple[str, ...] = ()
    gate_results: dict[str, str] = field(default_factory=dict)
    artifact_digests: tuple[str, ...] = ()


@dataclass
class DriftScanInput:
    """Everything Section 19.1 compares, in one value."""

    compiled: CompiledProject
    #: gate definitions as they currently exist on disk
    observed_gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    observed_gate_hashes: dict[str, str] = field(default_factory=dict)
    active_tasks: Sequence[ActiveTask] = ()
    artifacts: Sequence[ArtifactClaim] = ()
    test_claims: Sequence[EvaluationClaim] = ()
    release: ReleaseContents | None = None
    security_findings: Sequence[security.SecurityFinding] = ()
    #: components the dependency policy forbids reimplementing
    current_input_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class DriftReport:
    findings: list[Finding] = field(default_factory=list)
    security: security.SecurityScopeReport = field(default_factory=security.SecurityScopeReport)
    comparisons: dict[str, int] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocks]

    @property
    def observations(self) -> list[Finding]:
        return [f for f in self.findings if not f.blocks]

    @property
    def unresolved_scope_drift(self) -> int:
        """The ``auto_merge_requirements.unresolved_scope_drift`` value."""
        return len(self.blocking)

    def of_type(self, finding: DriftFinding) -> list[Finding]:
        return [f for f in self.findings if f.finding == str(finding)]

    def types_found(self) -> list[str]:
        return sorted({f.finding for f in self.findings})

    @property
    def terminal_state(self) -> ProjectState:
        return ProjectState.RUNNING if not self.blocking else ProjectState.FAILED_CONTRACT

    def as_body(self) -> dict[str, Any]:
        return {
            "comparisons": self.comparisons,
            "finding_count": len(self.findings),
            "blocking_count": len(self.blocking),
            "unresolved_scope_drift": self.unresolved_scope_drift,
            "types_found": self.types_found(),
            "findings": [f.as_body() for f in self.findings],
            "security": self.security.as_body(),
            "contract_ref": "contract.md#19.1,#19.2,#19.5",
        }


class DriftEngine:
    """Section 19.1's comparison, run as one deterministic pass."""

    def __init__(self, compiled: CompiledProject) -> None:
        self.compiled = compiled
        self.requirement_ids = {r.requirement_id for r in compiled.catalog.requirements}
        self.compiled_task_ids = set(compiled.tasks)
        self.sealed_names = self._sealed_names()
        self.prohibited_components = self._prohibited_components()
        self.role_incompatibilities = self._role_incompatibilities()

    # -- pack-derived policy ----------------------------------------------

    def _sealed_names(self) -> set[str]:
        """Sealed repository names, read from the compiled project path policy."""
        for obj in self.compiled.outputs.get("allowed_and_prohibited_paths", []):
            if obj.body.get("scope") == "project":
                return {str(name) for name in obj.body.get("sealed_repository_names", [])}
        return set()

    def _prohibited_components(self) -> set[str]:
        prohibited: set[str] = set()
        for node_id, node in self.compiled.graph.nodes.items():
            if node.kind == "SoftwarePackage" and node.attributes.get("prohibited"):
                prohibited.add(node_id.removeprefix("PKG:"))
        return prohibited

    def _role_incompatibilities(self) -> list[tuple[str, str, str]]:
        pairs: list[tuple[str, str, str]] = []
        for obj in self.compiled.outputs.get("role_separation", []):
            if obj.envelope.schema_id != "efah.role_incompatibility":
                continue
            roles = obj.body.get("roles", [])
            if len(roles) == 2 and obj.body.get("mandatory"):
                pairs.append((roles[0], roles[1], str(obj.body.get("rule"))))
        return pairs

    # -- the scan ----------------------------------------------------------

    def scan(self, scan_input: DriftScanInput) -> DriftReport:
        report = DriftReport()
        report.comparisons = {
            "compiled_requirements": len(self.requirement_ids),
            "compiled_tasks": len(self.compiled_task_ids),
            "observed_gates": len(scan_input.observed_gates),
            "active_tasks": len(scan_input.active_tasks),
            "artifacts": len(scan_input.artifacts),
            "test_claims": len(scan_input.test_claims),
            "release_tasks": len(scan_input.release.task_ids) if scan_input.release else 0,
            "security_findings": len(scan_input.security_findings),
        }
        self._compare_contract_to_requirements(scan_input, report)
        self._compare_gates(scan_input, report)
        self._compare_active_tasks(scan_input, report)
        self._compare_artifacts(scan_input, report)
        self._compare_test_claims(scan_input, report)
        self._compare_release(scan_input, report)
        self._apply_security_boundary(scan_input, report)
        return report

    # 1. approved contract vs compiled requirements
    def _compare_contract_to_requirements(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        if self.compiled.contract_version != CONTRACT_VERSION:
            report.findings.append(
                Finding(
                    finding=str(DriftFinding.STALE_CONTRACT_VERSION),
                    subject=self.compiled.contract_id,
                    detail=(
                        f"compiled against {self.compiled.contract_version}, governing version is {CONTRACT_VERSION}"
                    ),
                    contract_ref="contract.md#1.3,#19.2",
                )
            )
        for finding in self.compiled.blocking_findings:
            report.findings.append(
                Finding(
                    finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION)
                    if finding.kind == "COMPILED_TASK_NOT_IN_CONTRACT"
                    else finding.kind,
                    subject=finding.subject or self.compiled.project_id,
                    detail=finding.detail,
                    contract_ref="contract.md#8.1",
                )
            )

    # 2. compiled requirements vs project plan (the gate set that proves them)
    def _compare_gates(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        for gate_id, compiled_gate in sorted(self.compiled.gates.items()):
            observed = scan_input.observed_gates.get(gate_id)
            if observed is None:
                if scan_input.observed_gates:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.REQUIREMENT_WEAKENING),
                            subject=gate_id,
                            detail="gate is required by a compiled requirement but is absent from the observed gate set",
                            contract_ref="contract.md#14.3,#19.2",
                        )
                    )
                continue
            if compiled_gate.get("blocking") and not observed.get("blocking"):
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REQUIREMENT_WEAKENING),
                        subject=gate_id,
                        detail="gate was blocking in the approved contract and is no longer blocking",
                        contract_ref="contract.md#14.3",
                    )
                )
            compiled_assertions = {a["id"]: a for a in compiled_gate.get("assertions", [])}
            observed_assertions = {a["id"]: a for a in observed.get("assertions", [])}
            for assertion_id, assertion in compiled_assertions.items():
                seen = observed_assertions.get(assertion_id)
                if seen is None:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.REQUIREMENT_WEAKENING),
                            subject=f"{gate_id}.{assertion_id}",
                            detail="assertion removed from the gate",
                            contract_ref="contract.md#14.3",
                        )
                    )
                elif str(seen.get("expected")) != str(assertion.get("expected")):
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.REDEFINED_SUCCESS),
                            subject=f"{gate_id}.{assertion_id}",
                            detail=(
                                f"expected value changed from {assertion.get('expected')!r} "
                                f"to {seen.get('expected')!r}"
                            ),
                            contract_ref="contract.md#14.3,#19.2",
                        )
                    )

    # 3. project plan vs active tasks
    def _compare_active_tasks(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        by_paths: dict[str, list[str]] = {}
        for task in scan_input.active_tasks:
            linked = [r for r in task.requirement_ids if r in self.requirement_ids]
            if not linked:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNLINKED_TASK),
                        subject=task.task_id,
                        detail=(
                            "active task links to no compiled requirement"
                            if not task.requirement_ids
                            else f"requirement ids {list(task.requirement_ids)} are not in the compiled catalog"
                        ),
                        contract_ref="contract.md#8.1,#19.2",
                    )
                )
            if task.task_id not in self.compiled_task_ids:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                        subject=task.task_id,
                        detail="active task does not exist in the compiled project plan",
                        contract_ref="contract.md#19.2",
                    )
                )
            if task.contract_version != CONTRACT_VERSION:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.STALE_CONTRACT_VERSION),
                        subject=task.task_id,
                        detail=f"task is bound to contract {task.contract_version}, governing is {CONTRACT_VERSION}",
                        contract_ref="contract.md#9.4",
                    )
                )

            allowed = task.allowed_paths or tuple(
                self.compiled.tasks.get(task.task_id, {}).get("allowed_paths", [])
            )
            prohibited = task.prohibited_paths or tuple(
                self.compiled.tasks.get(task.task_id, {}).get("prohibited_paths", [])
            )
            for path in task.changed_paths:
                if self._touches_sealed(path):
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.PROTECTED_ASSET_ACCESS),
                            subject=task.task_id,
                            detail=f"changed path {path!r} reaches a sealed asset",
                            contract_ref="contract.md#17.2",
                            evidence=(path,),
                        )
                    )
                    continue
                if any(fnmatch(path, pattern) for pattern in prohibited):
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                            subject=task.task_id,
                            detail=f"changed path {path!r} matches a prohibited path pattern",
                            contract_ref="contract.md#9.4",
                            evidence=(path,),
                        )
                    )
                elif allowed and not any(fnmatch(path, pattern) for pattern in allowed):
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
                            subject=task.task_id,
                            detail=f"changed path {path!r} is outside the work unit's allowed paths",
                            contract_ref="contract.md#9.4",
                            evidence=(path,),
                        )
                    )
                by_paths.setdefault(path, []).append(task.task_id)

            for left, right, rule in self.role_incompatibilities:
                left_alias = task.role_assignments.get(left)
                right_alias = task.role_assignments.get(right)
                if left_alias and right_alias and left_alias == right_alias:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.ROLE_CONFLICT),
                            subject=task.task_id,
                            detail=f"alias {left_alias!r} holds both {left} and {right}; rule {rule}",
                            contract_ref="contract.md#12.2",
                        )
                    )

            if task.state in {str(s) for s in COMPLETION_CLAIM_STATES}:
                missing = [f for f in WIRING_FIELDS if not task.wiring_manifest.get(f)]
                if missing:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.MISSING_WIRING),
                            subject=task.task_id,
                            detail=f"task claims {task.state} with an incomplete wiring manifest: missing {missing}",
                            contract_ref="contract.md#5.2",
                        )
                    )

            for component in task.introduces_components:
                if component in self.prohibited_components and not task.build_vs_integrate_record:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.UNSUPPORTED_REIMPLEMENTATION),
                            subject=task.task_id,
                            detail=(
                                f"introduces {component!r}, which duplicates a selected dependency, "
                                "with no BUILD_VS_INTEGRATE record"
                            ),
                            contract_ref="contract.md#14.2,#28",
                        )
                    )

            for name, recorded in task.input_hashes.items():
                current = scan_input.current_input_hashes.get(name)
                if current is not None and current != recorded:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.STALE_INPUT_ARTIFACT),
                            subject=task.task_id,
                            detail=f"input {name!r} hashed {recorded} at claim time, now {current}",
                            contract_ref="contract.md#15.7",
                        )
                    )

        for path, owners in sorted(by_paths.items()):
            if len(set(owners)) > 1:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.DUPLICATE_OR_CONFLICTING_WORK),
                        subject=path,
                        detail=f"tasks {sorted(set(owners))} all change {path!r}",
                        contract_ref="contract.md#9.5,#19.2",
                    )
                )

    # 4. changed artifacts
    def _compare_artifacts(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        for artifact in scan_input.artifacts:
            if artifact.contract_version != CONTRACT_VERSION:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.STALE_CONTRACT_VERSION),
                        subject=artifact.artifact_id,
                        detail=f"artifact bound to contract {artifact.contract_version}",
                        contract_ref="contract.md#18",
                    )
                )
            for name, recorded in artifact.source_input_hashes.items():
                current = scan_input.current_input_hashes.get(name)
                if current is not None and current != recorded:
                    report.findings.append(
                        Finding(
                            finding=str(DriftFinding.STALE_INPUT_ARTIFACT),
                            subject=artifact.artifact_id,
                            detail=f"source {name!r} changed since the artifact was produced",
                            contract_ref="contract.md#15.7",
                        )
                    )
            if artifact.produced_by_task and artifact.produced_by_task not in self.compiled_task_ids:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                        subject=artifact.artifact_id,
                        detail=f"produced by {artifact.produced_by_task!r}, which is not a compiled task",
                        contract_ref="contract.md#19.2",
                    )
                )

    # 5. test and evaluation claims
    def _compare_test_claims(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        for claim in scan_input.test_claims:
            expected_hash = scan_input.observed_gate_hashes.get(claim.gate_id)
            if expected_hash and claim.assertion_hash and claim.assertion_hash != expected_hash:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REDEFINED_SUCCESS),
                        subject=claim.gate_id,
                        detail=(
                            f"claim ran against assertion hash {claim.assertion_hash}, "
                            f"the pinned gate hashes to {expected_hash}"
                        ),
                        contract_ref="contract.md#14.3",
                    )
                )
            gate = self.compiled.gates.get(claim.gate_id)
            if gate is None:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                        subject=claim.gate_id,
                        detail="evaluation claim references a gate the contract does not define",
                        contract_ref="contract.md#19.2",
                    )
                )
                continue
            if claim.model_judge_in_verdict_path and not gate.get("model_judge_in_verdict_path", False):
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REDEFINED_SUCCESS),
                        subject=claim.gate_id,
                        detail="a model judge entered a verdict path the gate declares deterministic",
                        contract_ref="contract.md#17.4",
                    )
                )
            if claim.verdict == "PASS" and not (claim.evidence or claim.oracle_version):
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REDEFINED_SUCCESS),
                        subject=claim.gate_id,
                        detail="PASS claimed with no named evidence and no oracle version (Section 18)",
                        contract_ref="contract.md#18",
                    )
                )

    # 6. release contents
    def _compare_release(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        release = scan_input.release
        if release is None:
            return
        for task_id in release.task_ids:
            if task_id not in self.compiled_task_ids:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION),
                        subject=release.commit or "release",
                        detail=f"release contains task {task_id!r} that is not in the compiled plan",
                        contract_ref="contract.md#19.1,#21.2",
                    )
                )
        for gate_id, gate in sorted(self.compiled.gates.items()):
            if not gate.get("blocking"):
                continue
            verdict = release.gate_results.get(gate_id)
            if verdict is None:
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REQUIREMENT_WEAKENING),
                        subject=gate_id,
                        detail="release reports no result for a blocking gate",
                        contract_ref="contract.md#21.2",
                    )
                )
            elif verdict != "PASS":
                report.findings.append(
                    Finding(
                        finding=str(DriftFinding.REDEFINED_SUCCESS),
                        subject=gate_id,
                        detail=f"release carries verdict {verdict!r} for a blocking gate",
                        contract_ref="contract.md#21.2",
                    )
                )

    # 7. Section 19.5 security boundary
    def _apply_security_boundary(self, scan_input: DriftScanInput, report: DriftReport) -> None:
        approved = self.approved_security_refs()
        report.security = security.admit(scan_input.security_findings, approved)
        for classification in report.security.blocking:
            report.findings.append(
                Finding(
                    finding=str(DriftFinding.UNAPPROVED_SCOPE_EXPANSION)
                    if not classification.admitted_refs
                    else "SECURITY_FINDING_IN_SCOPE",
                    subject=classification.finding_id,
                    detail=classification.rationale,
                    blocks=True,
                    contract_ref="contract.md#19.5",
                    evidence=classification.admitted_refs,
                )
            )
        for classification in report.security.observations:
            report.findings.append(
                Finding(
                    finding=str(DriftFinding.OUT_OF_SCOPE_OBSERVATION),
                    subject=classification.finding_id,
                    detail=classification.rationale,
                    blocks=False,
                    contract_ref="contract.md#19.5",
                )
            )
        for expansion in report.security.expansions:
            report.findings.append(
                Finding(
                    finding=str(DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION),
                    subject=expansion["finding_id"],
                    detail=expansion["detail"],
                    blocks=True,
                    contract_ref="contract.md#19.5,#26",
                )
            )

    def approved_security_refs(self) -> set[str]:
        """Requirement, threat, risk and policy ids a finding may map to."""
        refs = set(self.requirement_ids)
        refs |= {gate_id for gate_id in self.compiled.gates}
        for requirement in self.compiled.catalog.requirements:
            refs.update(requirement.contract_refs)
        return {r for r in refs if r}

    def _touches_sealed(self, path: str) -> bool:
        return any(name and (path.startswith(f"{name}/") or f"/{name}/" in path) for name in self.sealed_names)


def scan(compiled: CompiledProject, **kwargs: Any) -> DriftReport:
    """Convenience wrapper: build a scan input and run one pass."""
    return DriftEngine(compiled).scan(DriftScanInput(compiled=compiled, **kwargs))
