"""Known-good, known-bad, gaming-probe, and UNVERIFIABLE fixtures.

Contract Section 17.4 requires all four for every trusted oracle. The fixture
IDs here are the IDs in the pack's oracle definitions -- ``KG-001``, ``KB-003``,
``GP-002`` -- so a definition entry with no fixture behind it is detectable
rather than aspirational. :func:`missing_fixture_ids` is what detects it, and
:mod:`oracles.minting` refuses to mint an oracle whose definition promises a
fixture that does not exist.

The gaming probes matter more than the known-bad cases. A known-bad case is a
mistake; a gaming probe is what a builder does on purpose when it cannot get a
check to go green honestly. Each probe here is a way to make the oracle say
PASS while the underlying property is false, and each one must still FAIL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from governance.envelope import CompiledObject, EvidenceTier, content_hash
from governance.states import DriftFinding, TaskState, Verdict
from oracles.base import DeterministicOracle
from oracles.oracle_001_composition import CompositionSnapshot, EntryPoint, ModuleWiring
from oracles.oracle_002_lease_fencing import FencingSubject, LeaseRecord, Submission
from oracles.oracle_003_provenance import (
    ClaimedResult,
    EvidenceArtifactRef,
    ExecutedTestRecord,
    ProvenanceSubject,
    recompute_header_hash,
)

FIXTURE_DATA_DIR = Path(__file__).resolve().parent / "fixture_data"
KNOWN_GOOD_EVIDENCE = FIXTURE_DATA_DIR / "oracle-003-known-good-evidence.json"

KNOWN_GOOD = "known_good"
KNOWN_BAD = "known_bad"
GAMING_PROBE = "gaming_probe"
UNVERIFIABLE_PROBE = "unverifiable_probe"

_T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


@dataclass
class Fixture:
    fixture_id: str
    oracle_id: str
    kind: str
    description: str
    expected_verdict: Verdict
    subject: Any = None
    expected_failure_state: TaskState | DriftFinding | None = None
    #: KB-004 for ORACLE-002 is a race, not a single subject.
    concurrent_subjects: list[Any] | None = None
    expected_concurrent_pass_count: int | None = None


@dataclass
class FixtureOutcome:
    fixture_id: str
    kind: str
    expected: str
    observed: str
    ok: bool
    detail: str = ""


@dataclass
class FixtureSuiteResult:
    oracle_id: str
    outcomes: list[FixtureOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.outcomes)

    @property
    def summary(self) -> str:
        return "PASS" if self.ok else "FAIL"

    def failures(self) -> list[FixtureOutcome]:
        return [o for o in self.outcomes if not o.ok]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "oracle_id": self.oracle_id,
            "result": self.summary,
            "total": len(self.outcomes),
            "failed": len(self.failures()),
            "outcomes": [
                {
                    "fixture_id": o.fixture_id,
                    "kind": o.kind,
                    "expected": o.expected,
                    "observed": o.observed,
                    "ok": o.ok,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


# ---------------------------------------------------------------------------
# ORACLE-001 — composition reachability
# ---------------------------------------------------------------------------

def _wiring(name: str) -> ModuleWiring:
    return ModuleWiring(
        provides=[f"{name}.application_interface"],
        consumes=[],
        startup_registration=True,
        configuration_schema=f"config/{name}.schema.json",
        health_check=f"GET /health/{name}",
        integration_test=f"tests/integration/test_{name}.py",
        e2e_path=f"harness project run -> api -> {name} -> result",
        telemetry_span=f"efah.{name}",
        dashboard_projection=f"plane.view.{name}",
    )


def good_composition() -> CompositionSnapshot:
    modules = ["api", "projects", "tasks", "evaluation"]
    return CompositionSnapshot(
        composition_root_parseable=True,
        declared_modules=modules,
        wiring={m: _wiring(m) for m in modules},
        registered_modules=list(modules),
        entry_points=[
            EntryPoint(
                name="harness project run",
                approved_user_to_result_path=True,
                reaches=["api"],
            ),
        ],
        invocation_edges=[
            ("api", "projects"),
            ("projects", "tasks"),
            ("tasks", "evaluation"),
        ],
        import_edges=[
            ("api", "projects"),
            ("projects", "tasks"),
            ("tasks", "evaluation"),
        ],
    )


def _oracle_001_fixtures() -> list[Fixture]:
    out: list[Fixture] = []

    out.append(
        Fixture(
            "KG-001",
            "ORACLE-001",
            KNOWN_GOOD,
            "All modules registered and reachable from the API entry point.",
            Verdict.PASS,
            good_composition(),
        )
    )

    kb1 = good_composition()
    kb1.registered_modules.remove("evaluation")
    out.append(
        Fixture(
            "KB-001",
            "ORACLE-001",
            KNOWN_BAD,
            "Module with passing unit tests but absent from the composition root.",
            Verdict.FAIL,
            kb1,
            TaskState.FAILED_WIRING,
        )
    )

    kb2 = good_composition()
    kb2.invocation_edges = [("api", "projects"), ("projects", "tasks")]
    kb2.import_edges = list(kb2.invocation_edges)
    out.append(
        Fixture(
            "KB-002",
            "ORACLE-001",
            KNOWN_BAD,
            "Module registered but unreachable from any declared entry point.",
            Verdict.FAIL,
            kb2,
            TaskState.FAILED_WIRING,
        )
    )

    kb3 = good_composition()
    kb3.infrastructure_imports = [("tasks", "projects")]
    out.append(
        Fixture(
            "KB-003",
            "ORACLE-001",
            KNOWN_BAD,
            "Module importing another module's infrastructure implementation directly.",
            Verdict.FAIL,
            kb3,
            TaskState.FAILED_SCOPE,
        )
    )

    kb4 = good_composition()
    kb4.import_edges = kb4.import_edges + [("evaluation", "projects")]
    out.append(
        Fixture(
            "KB-004",
            "ORACLE-001",
            KNOWN_BAD,
            "Circular dependency between two domain modules.",
            Verdict.FAIL,
            kb4,
            TaskState.FAILED_SCOPE,
        )
    )

    gp1 = good_composition()
    gp1.invocation_edges = [("api", "projects"), ("projects", "tasks")]
    gp1.import_edges = list(gp1.invocation_edges)
    out.append(
        Fixture(
            "GP-001",
            "ORACLE-001",
            GAMING_PROBE,
            "Register the module at the composition root but never call it.",
            Verdict.FAIL,
            gp1,
            TaskState.FAILED_WIRING,
        )
    )

    gp2 = good_composition()
    gp2.invocation_edges = [("api", "projects"), ("projects", "tasks")]
    gp2.import_edges = list(gp2.invocation_edges)
    gp2.entry_points = gp2.entry_points + [
        EntryPoint(
            name="tests/e2e/_test_only_entry.py",
            approved_user_to_result_path=False,
            reaches=["evaluation"],
        )
    ]
    out.append(
        Fixture(
            "GP-002",
            "ORACLE-001",
            GAMING_PROBE,
            "Add a test-only entry point that reaches the module.",
            Verdict.FAIL,
            gp2,
            TaskState.FAILED_WIRING,
        )
    )

    gp3 = good_composition()
    gp3.wiring["evaluation"].health_check = "TODO"
    gp3.wiring["evaluation"].e2e_path = ""
    out.append(
        Fixture(
            "GP-003",
            "ORACLE-001",
            GAMING_PROBE,
            "Declare the wiring manifest fields with empty or placeholder values.",
            Verdict.FAIL,
            gp3,
            TaskState.FAILED_WIRING,
        )
    )

    gp4 = good_composition()
    gp4.invocation_edges = [("api", "projects"), ("projects", "tasks")]
    gp4.import_edges = list(gp4.invocation_edges)
    gp4.excluded_modules = {"evaluation": None}
    out.append(
        Fixture(
            "GP-004",
            "ORACLE-001",
            GAMING_PROBE,
            "Mark the module as excluded from reachability analysis.",
            Verdict.FAIL,
            gp4,
            TaskState.FAILED_SCOPE,
        )
    )

    up1 = good_composition()
    up1.composition_root_parseable = False
    out.append(
        Fixture(
            "UP-composition_root_not_parseable",
            "ORACLE-001",
            UNVERIFIABLE_PROBE,
            "composition_root_not_parseable",
            Verdict.UNVERIFIABLE,
            up1,
        )
    )

    up2 = good_composition()
    up2.entry_points = []
    out.append(
        Fixture(
            "UP-entry_points_undeclared",
            "ORACLE-001",
            UNVERIFIABLE_PROBE,
            "entry_points_undeclared",
            Verdict.UNVERIFIABLE,
            up2,
        )
    )

    up3 = good_composition()
    del up3.wiring["evaluation"]
    out.append(
        Fixture(
            "UP-wiring_manifest_absent_for_one_or_more_modules",
            "ORACLE-001",
            UNVERIFIABLE_PROBE,
            "wiring_manifest_absent_for_one_or_more_modules",
            Verdict.UNVERIFIABLE,
            up3,
        )
    )
    return out


# ---------------------------------------------------------------------------
# ORACLE-002 — lease generation fencing
# ---------------------------------------------------------------------------

def good_lease() -> LeaseRecord:
    return LeaseRecord(
        work_unit_id="WU-040",
        lease_id="lease-040",
        generation=4,
        holder_alias="implementer-i12",
        expires_at=_T0 + timedelta(minutes=30),
        branch="feat/ws-e-assurance",
        worktree="/wt/ws-e-assurance",
        input_hashes={"contract.yaml": "sha256:aaa", "task.json": "sha256:bbb"},
        superseded_generations={3: _T0 - timedelta(minutes=5)},
    )


def good_submission(**overrides: Any) -> Submission:
    base: dict[str, Any] = {
        "submission_id": "sub-1",
        "work_unit_id": "WU-040",
        "lease_id": "lease-040",
        "generation": 4,
        "submitter_alias": "implementer-i12",
        "branch": "feat/ws-e-assurance",
        "worktree": "/wt/ws-e-assurance",
        "input_hashes": {"contract.yaml": "sha256:aaa", "task.json": "sha256:bbb"},
    }
    base.update(overrides)
    return Submission(**base)


def good_fencing(**overrides: Any) -> FencingSubject:
    subject = FencingSubject(
        submission=good_submission(),
        lease=good_lease(),
        observed_at=_T0,
    )
    for key, value in overrides.items():
        setattr(subject, key, value)
    return subject


def _oracle_002_fixtures() -> list[Fixture]:
    out: list[Fixture] = []

    out.append(
        Fixture(
            "KG-001",
            "ORACLE-002",
            KNOWN_GOOD,
            "Current generation, unexpired lease, matching worktree.",
            Verdict.PASS,
            good_fencing(),
        )
    )

    expired = good_lease()
    expired.expires_at = _T0 - timedelta(minutes=1)
    out.append(
        Fixture(
            "KB-001",
            "ORACLE-002",
            KNOWN_BAD,
            "Worker submits after its lease expired and was reassigned.",
            Verdict.FAIL,
            good_fencing(lease=expired),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "KB-002",
            "ORACLE-002",
            KNOWN_BAD,
            "Worker holds generation 3 while current generation is 4.",
            Verdict.FAIL,
            good_fencing(submission=good_submission(generation=3)),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "KB-003",
            "ORACLE-002",
            KNOWN_BAD,
            "Submission targets a branch owned by a different lease.",
            Verdict.FAIL,
            good_fencing(submission=good_submission(branch="feat/ws-a-compiler")),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    racer_a = good_fencing(submission=good_submission(submission_id="sub-a"))
    racer_b = good_fencing(submission=good_submission(submission_id="sub-b"))
    out.append(
        Fixture(
            "KB-004",
            "ORACLE-002",
            KNOWN_BAD,
            "Two workers submit concurrently for the same work unit.",
            Verdict.PASS,
            None,
            None,
            concurrent_subjects=[racer_a, racer_b],
            expected_concurrent_pass_count=1,
        )
    )

    # GP-001 — renewal after the generation died does not resurrect it.
    renewed = good_lease()
    renewed.generation = 3
    renewed.expires_at = _T0 + timedelta(minutes=30)
    renewed.superseded_generations = {3: _T0 - timedelta(minutes=5)}
    out.append(
        Fixture(
            "GP-001",
            "ORACLE-002",
            GAMING_PROBE,
            "Renew the lease at submission time to make it look current.",
            Verdict.FAIL,
            good_fencing(lease=renewed, submission=good_submission(generation=3)),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "GP-002",
            "ORACLE-002",
            GAMING_PROBE,
            "Submit with the current generation number copied from the ledger.",
            Verdict.FAIL,
            good_fencing(
                submission=good_submission(
                    submitter_alias="implementer-i99",
                    input_hashes={"contract.yaml": "sha256:zzz"},
                )
            ),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "GP-003",
            "ORACLE-002",
            GAMING_PROBE,
            "Backdate the submission timestamp to before expiry.",
            Verdict.FAIL,
            good_fencing(
                lease=expired,
                submission=good_submission(
                    claimed_submitted_at=_T0 - timedelta(minutes=10)
                ),
            ),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "GP-004",
            "ORACLE-002",
            GAMING_PROBE,
            "Force-push the stale worker's branch over the current one.",
            Verdict.FAIL,
            good_fencing(
                submission=good_submission(
                    generation=3, submitter_alias="implementer-i99"
                )
            ),
            TaskState.STALE_ASSIGNMENT,
        )
    )

    out.append(
        Fixture(
            "UP-lease_record_absent_from_terminusdb",
            "ORACLE-002",
            UNVERIFIABLE_PROBE,
            "lease_record_absent_from_terminusdb",
            Verdict.UNVERIFIABLE,
            good_fencing(lease=None),
        )
    )
    out.append(
        Fixture(
            "UP-submission_carries_no_lease_identifier",
            "ORACLE-002",
            UNVERIFIABLE_PROBE,
            "submission_carries_no_lease_identifier",
            Verdict.UNVERIFIABLE,
            good_fencing(submission=good_submission(lease_id=None, generation=None)),
        )
    )
    ambiguous = good_lease()
    ambiguous.expires_at = _T0 + timedelta(seconds=5)
    out.append(
        Fixture(
            "UP-clock_skew_exceeds_tolerance_and_expiry_is_ambiguous",
            "ORACLE-002",
            UNVERIFIABLE_PROBE,
            "clock_skew_exceeds_tolerance_and_expiry_is_ambiguous",
            Verdict.UNVERIFIABLE,
            good_fencing(lease=ambiguous, clock_skew_observed_seconds=90.0),
        )
    )
    return out


# ---------------------------------------------------------------------------
# ORACLE-003 — provenance binding
# ---------------------------------------------------------------------------

def known_good_evidence_ref(**overrides: Any) -> EvidenceArtifactRef:
    raw = KNOWN_GOOD_EVIDENCE.read_bytes()
    ref = EvidenceArtifactRef(
        name="oracle_003_known_good_evidence",
        path=str(KNOWN_GOOD_EVIDENCE),
        exists=KNOWN_GOOD_EVIDENCE.is_file(),
        readable=True,
        declared_content_hash=content_hash(raw),
        recomputed_content_hash=content_hash(raw),
        structured=True,
    )
    for key, value in overrides.items():
        setattr(ref, key, value)
    return ref


def _good_test_record() -> ExecutedTestRecord:
    return ExecutedTestRecord(
        command="pytest tests/contract/test_oracle_003_provenance.py -q",
        environment="python3.12 / linux",
        timestamp=_T0.isoformat(),
        exit_status=0,
        raw_result_artifact="evidence/gates/ORACLE-003/pytest.json",
        commit_binding="0f00a7a",
    )


def good_claimed_result(contract_version: str = "1.1", **overrides: Any) -> ClaimedResult:
    obj = CompiledObject.create(
        schema_id="efah.gate_result",
        created_by_alias="oracle-o02",
        body={"gate_id": "GATE-D2-20", "verdict": "PASS"},
    )
    header = obj.envelope.model_dump(mode="json")
    if contract_version != header["contract_version"]:
        header["contract_version"] = contract_version
        header["content_hash"] = recompute_header_hash(header, obj.body)
    result = ClaimedResult(
        result_id="R-001",
        header=header,
        body=obj.body,
        evidence_artifacts=[known_good_evidence_ref()],
        evidence_tier=EvidenceTier.DETERMINISTIC_ORACLE.value,
        verdict_path="deterministic_oracle",
        test_record=_good_test_record(),
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def good_provenance(**overrides: Any) -> ProvenanceSubject:
    subject = ProvenanceSubject(
        results=[good_claimed_result()],
        current_contract_version="1.1",
    )
    for key, value in overrides.items():
        setattr(subject, key, value)
    return subject


def _oracle_003_fixtures() -> list[Fixture]:
    out: list[Fixture] = []

    out.append(
        Fixture(
            "KG-001",
            "ORACLE-003",
            KNOWN_GOOD,
            "Result with full header, matching hash, resolvable commits, readable evidence.",
            Verdict.PASS,
            good_provenance(),
        )
    )

    out.append(
        Fixture(
            "KB-001",
            "ORACLE-003",
            KNOWN_BAD,
            "Result claiming success with no named evidence artifact.",
            Verdict.FAIL,
            good_provenance(results=[good_claimed_result(evidence_artifacts=[])]),
            TaskState.FAILED_PROVENANCE,
        )
    )

    tampered = good_claimed_result()
    tampered.body = dict(tampered.body) | {"verdict": "FAIL"}
    out.append(
        Fixture(
            "KB-002",
            "ORACLE-003",
            KNOWN_BAD,
            "content_hash does not match the recomputed object body.",
            Verdict.FAIL,
            good_provenance(results=[tampered]),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "KB-003",
            "ORACLE-003",
            KNOWN_BAD,
            "Result bound to contract version 0.2 while the current version is live.",
            Verdict.FAIL,
            good_provenance(results=[good_claimed_result(contract_version="0.2")]),
            DriftFinding.STALE_CONTRACT_VERSION,
        )
    )

    out.append(
        Fixture(
            "KB-004",
            "ORACLE-003",
            KNOWN_BAD,
            "Test record with no command, exit status, or commit binding.",
            Verdict.FAIL,
            good_provenance(
                results=[
                    good_claimed_result(
                        test_record=ExecutedTestRecord(environment="linux", timestamp=_T0.isoformat())
                    )
                ]
            ),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "KB-005",
            "ORACLE-003",
            KNOWN_BAD,
            "Evidence tier field absent or outside the permitted five.",
            Verdict.FAIL,
            good_provenance(results=[good_claimed_result(evidence_tier=None)]),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "GP-001",
            "ORACLE-003",
            GAMING_PROBE,
            "Point the evidence field at a file that does not exist.",
            Verdict.FAIL,
            good_provenance(
                results=[
                    good_claimed_result(
                        evidence_artifacts=[
                            known_good_evidence_ref(
                                path="/nonexistent/evidence.json", exists=False, readable=False
                            )
                        ]
                    )
                ]
            ),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "GP-002",
            "ORACLE-003",
            GAMING_PROBE,
            "Copy a valid content_hash from a different artifact.",
            Verdict.FAIL,
            good_provenance(
                results=[
                    good_claimed_result(
                        evidence_artifacts=[
                            known_good_evidence_ref(
                                declared_content_hash=content_hash(b"a different artifact")
                            )
                        ]
                    )
                ]
            ),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "GP-003",
            "ORACLE-003",
            GAMING_PROBE,
            "Claim DETERMINISTIC_ORACLE tier for a model-judged result.",
            Verdict.FAIL,
            good_provenance(results=[good_claimed_result(verdict_path="model_judge")]),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "GP-004",
            "ORACLE-003",
            GAMING_PROBE,
            "Re-run tests on a different commit than the one submitted.",
            Verdict.FAIL,
            good_provenance(
                results=[good_claimed_result(repository_commit_resolvable=False)]
            ),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "GP-005",
            "ORACLE-003",
            GAMING_PROBE,
            "Write a prose summary in place of the evidence package.",
            Verdict.FAIL,
            good_provenance(
                results=[
                    good_claimed_result(
                        evidence_artifacts=[
                            known_good_evidence_ref(
                                name="summary.md",
                                structured=False,
                                declared_content_hash=None,
                            )
                        ]
                    )
                ]
            ),
            TaskState.FAILED_PROVENANCE,
        )
    )

    out.append(
        Fixture(
            "UP-terminus_commit_unreachable_due_to_service_outage",
            "ORACLE-003",
            UNVERIFIABLE_PROBE,
            "terminus_commit_unreachable_due_to_service_outage",
            Verdict.UNVERIFIABLE,
            good_provenance(terminus_service_available=False),
        )
    )
    out.append(
        Fixture(
            "UP-evidence_artifact_storage_unavailable",
            "ORACLE-003",
            UNVERIFIABLE_PROBE,
            "evidence_artifact_storage_unavailable",
            Verdict.UNVERIFIABLE,
            good_provenance(evidence_storage_available=False),
        )
    )
    return out


_BUILDERS = {
    "ORACLE-001": _oracle_001_fixtures,
    "ORACLE-002": _oracle_002_fixtures,
    "ORACLE-003": _oracle_003_fixtures,
}


def fixtures_for(oracle_id: str) -> list[Fixture]:
    return _BUILDERS[oracle_id]()


def declared_fixture_ids(definition: dict[str, Any]) -> set[str]:
    """Every fixture and probe ID the pack definition promises exists."""
    ids: set[str] = set()
    fixtures = definition.get("fixtures") or {}
    for group in ("known_good", "known_bad"):
        for entry in fixtures.get(group) or []:
            ids.add(str(entry["id"]))
    for probe in definition.get("gaming_probes") or []:
        ids.add(str(probe["id"]))
    return ids


def missing_fixture_ids(definition: dict[str, Any]) -> list[str]:
    have = {f.fixture_id for f in fixtures_for(definition["oracle_id"])}
    return sorted(declared_fixture_ids(definition) - have)


def run_fixture_suite(
    oracle: DeterministicOracle, fixtures: list[Fixture] | None = None
) -> FixtureSuiteResult:
    """Run every fixture through the oracle's real verdict path."""
    fixtures = fixtures if fixtures is not None else fixtures_for(oracle.oracle_id)
    result = FixtureSuiteResult(oracle_id=oracle.oracle_id)
    for fixture in fixtures:
        if fixture.concurrent_subjects is not None:
            decisions = oracle.decide_concurrent(fixture.concurrent_subjects)  # type: ignore[attr-defined]
            passes = sum(1 for d in decisions if d.verdict is Verdict.PASS)
            ok = passes == fixture.expected_concurrent_pass_count
            result.outcomes.append(
                FixtureOutcome(
                    fixture.fixture_id,
                    fixture.kind,
                    f"exactly {fixture.expected_concurrent_pass_count} PASS",
                    f"{passes} PASS of {len(decisions)}",
                    ok,
                    fixture.description,
                )
            )
            continue

        decision = oracle.decide(fixture.subject)
        expected = fixture.expected_verdict.value
        if fixture.expected_failure_state is not None:
            expected = f"{expected}/{fixture.expected_failure_state.value}"
        observed = decision.verdict.value
        if decision.failure_state is not None:
            observed = f"{observed}/{decision.failure_state.value}"
        ok = decision.verdict is fixture.expected_verdict
        if ok and fixture.expected_failure_state is not None:
            ok = decision.failure_state == fixture.expected_failure_state
        result.outcomes.append(
            FixtureOutcome(
                fixture.fixture_id,
                fixture.kind,
                expected,
                observed,
                ok,
                "; ".join(decision.reasons)[:400],
            )
        )
    return result
