"""Test mutants and workflow/governance mutants.

Contract Section 17.1 lists four mutant sets. :mod:`mutants.catalog` covers the
implementation and evaluator/oracle sets; this module covers the other two,
which act on the *rules* rather than on the code:

* a **test mutant** weakens a visible behavioural assertion. Contract Section
  14.3 hashes those assertions before convergence precisely so this mutation is
  detectable, and ``ASSERTION_HASHES.txt`` is what kills it.
* a **workflow/governance mutant** weakens a rule of the harness itself -- a
  gate that admits a model judge, an auto-merge that skips a requirement or
  lets the implementing agent merge its own work, a verifier response that
  smuggles holdout content, a client that invents an endpoint rather than
  blocking, an evaluation whose lanes drift onto different commits.

Every mutation here is applied for real, against a throwaway copy of the gate
directory where a file is involved, and the kill is the harness's own refusal.
None of these are checked by asking whether the rule is written down.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from evaluation.auto_merge import (
    AUTO_MERGE_REQUIREMENTS,
    REQUIRED_VALUES,
    AutoMergeEvaluation,
    RequirementNotEvaluated,
)
from evaluation.binding import CandidateBinding, EvaluationSet, Lane, LaneRun
from evaluation.gate_spec import GATE_DIR, ModelJudgeInVerdictPath, load_gate
from evaluation.verifier_client import (
    ProtectedVerifierClient,
    VerifierEndpointConfig,
    build_submission,
    validate_response,
)
from governance.envelope import KnowledgeTier
from governance.states import ProjectState, TaskState, Verdict
from knowledge.tiers import PromotionRejected, admit_agent_output, promote
from mutants.catalog import KillReport, Mutant, MutantClass

MANIFEST = GATE_DIR / "ASSERTION_HASHES.txt"


def _manifest_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in MANIFEST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("sha256:"):
            digest, _, name = line.partition("  ")
            hashes[name.strip()] = digest[len("sha256:"):]
    return hashes


def _copy_gate_dir(destination: Path) -> Path:
    shutil.copytree(GATE_DIR, destination / "visible")
    return destination / "visible"


# ---------------------------------------------------------------------------
# Test mutants
# ---------------------------------------------------------------------------

def _weaken_an_assertion(_: dict[str, Any]) -> KillReport:
    """Weaken GATE-D2-20 A2 from 'zero model calls' to 'few model calls'."""
    target_name = "GATE-D2-20-oracle-health-and-no-judge-in-the-determinis.yaml"
    expected = _manifest_hashes().get(target_name)
    if expected is None:
        return KillReport(False, f"{target_name} is absent from the assertion manifest")

    with tempfile.TemporaryDirectory() as tmp:
        working = _copy_gate_dir(Path(tmp))
        target = working / target_name
        text = target.read_text()
        weakened = text.replace(
            "expected: zero_model_calls_in_verdict_path",
            "expected: few_model_calls_in_verdict_path",
        )
        if weakened == text:
            return KillReport(False, "the assertion this mutant weakens was not found")
        target.write_text(weakened)
        observed = hashlib.sha256(target.read_bytes()).hexdigest()
        killed = observed != expected
        return KillReport(
            killed,
            f"assertion-hash manifest {'detected' if killed else 'missed'} the weakened "
            f"assertion ({observed[:12]} vs pinned {expected[:12]})",
        )


def _delete_an_assertion(_: dict[str, Any]) -> KillReport:
    """Delete GATE-D3-24 A5 entirely and see whether the manifest notices."""
    target_name = "GATE-D3-24-known-bad-mutant-is-rejected.yaml"
    expected = _manifest_hashes().get(target_name)
    if expected is None:
        return KillReport(False, f"{target_name} is absent from the assertion manifest")

    with tempfile.TemporaryDirectory() as tmp:
        working = _copy_gate_dir(Path(tmp))
        target = working / target_name
        lines = target.read_text().splitlines(keepends=True)
        kept = [line for line in lines if "A5" not in line]
        if len(kept) == len(lines):
            return KillReport(False, "assertion A5 was not found to delete")
        target.write_text("".join(kept))
        observed = hashlib.sha256(target.read_bytes()).hexdigest()
        killed = observed != expected
        return KillReport(
            killed,
            f"assertion-hash manifest {'detected' if killed else 'missed'} the deleted assertion",
        )


# ---------------------------------------------------------------------------
# Workflow / governance mutants
# ---------------------------------------------------------------------------

def _gate_admits_a_model_judge(_: dict[str, Any]) -> KillReport:
    """Flip ``model_judge_in_verdict_path`` to true on a real gate and load it."""
    target_name = "GATE-D2-20-oracle-health-and-no-judge-in-the-determinis.yaml"
    with tempfile.TemporaryDirectory() as tmp:
        working = _copy_gate_dir(Path(tmp))
        target = working / target_name
        target.write_text(
            target.read_text().replace(
                "model_judge_in_verdict_path: false", "model_judge_in_verdict_path: true"
            )
        )
        parsed = yaml.safe_load(target.read_text())
        if parsed.get("model_judge_in_verdict_path") is not True:
            return KillReport(False, "the mutation did not apply")
        try:
            load_gate(target)
        except ModelJudgeInVerdictPath as exc:
            return KillReport(True, f"gate loader refused the mutated gate: {exc}")
        return KillReport(False, "the gate loader accepted a gate that admits a model judge")


def _auto_merge_skips_a_requirement(_: dict[str, Any]) -> KillReport:
    """Record twelve of the thirteen Section 21.2 requirements."""
    evaluation = AutoMergeEvaluation(pull_request_ref="PR-mutant", candidate_commit="0" * 40)
    for name in AUTO_MERGE_REQUIREMENTS[:-1]:
        evaluation.record(name, REQUIRED_VALUES[name], source="mutant")
    evaluation.merge_actor = "ci"
    evaluation.implementing_agent_alias = "implementer-i12"
    evaluation.ci_checks_present = True

    allowed, blockers = evaluation.may_merge()
    try:
        evaluation.verdict()
    except RequirementNotEvaluated as exc:
        return KillReport(True, f"composite refused to decide with a requirement missing: {exc}")
    return KillReport(
        not allowed,
        f"may_merge={allowed}, blockers={blockers}",
    )


def _auto_merge_tolerates_protected_access(_: dict[str, Any]) -> KillReport:
    """All thirteen recorded, but a protected asset was touched."""
    evaluation = AutoMergeEvaluation(pull_request_ref="PR-mutant", candidate_commit="0" * 40)
    for name in AUTO_MERGE_REQUIREMENTS:
        evaluation.record(name, REQUIRED_VALUES[name], source="mutant")
    evaluation.record("protected_assets_accessed", True, source="mutant")
    evaluation.merge_actor = "ci"
    evaluation.implementing_agent_alias = "implementer-i12"
    evaluation.ci_checks_present = True

    allowed, blockers = evaluation.may_merge()
    killed = (not allowed) and evaluation.verdict() is Verdict.FAIL
    return KillReport(killed, f"may_merge={allowed}, verdict={evaluation.verdict().value}, {blockers}")


def _implementing_agent_self_merges(_: dict[str, Any]) -> KillReport:
    """A fully green PR merged by the agent that wrote it (GATE-D3-25 A4)."""
    evaluation = AutoMergeEvaluation(pull_request_ref="PR-mutant", candidate_commit="0" * 40)
    for name in AUTO_MERGE_REQUIREMENTS:
        evaluation.record(name, REQUIRED_VALUES[name], source="mutant")
    evaluation.merge_actor = "implementer-i12"
    evaluation.implementing_agent_alias = "implementer-i12"
    evaluation.ci_checks_present = True

    allowed, blockers = evaluation.may_merge()
    return KillReport(not allowed, f"may_merge={allowed}, blockers={blockers}")


def _verifier_smuggles_holdout_content(_: dict[str, Any]) -> KillReport:
    """A correctly-shaped response whose health carries a holdout case body."""
    payload = {
        "evaluation_request_id": "EVAL-1",
        "verdict": "FAIL",
        "oracle_version": "1.0.0",
        "oracle_health": {
            "oracle_id": "ORACLE-003",
            "hidden_case_body": "def test_holdout():\n    assert compute(7) == 42\n",
        },
        "failure_class": "TEST_FAILURE",
    }
    result, findings = validate_response(payload)
    killed = result is None and bool(findings)
    return KillReport(killed, f"client findings: {findings}")


def _verifier_returns_extra_field(_: dict[str, Any]) -> KillReport:
    """A sixth field on the response shape."""
    payload = {
        "evaluation_request_id": "EVAL-1",
        "verdict": "FAIL",
        "oracle_version": "1.0.0",
        "oracle_health": {"oracle_id": "ORACLE-003"},
        "failure_class": "TEST_FAILURE",
        "failed_assertion_text": "expected reachability from an approved entry point",
    }
    result, findings = validate_response(payload)
    killed = result is None and bool(findings)
    return KillReport(killed, f"client findings: {findings}")


def _client_invents_an_endpoint(_: dict[str, Any]) -> KillReport:
    """An unconfigured client must block, not guess a route or fall back locally."""
    client = ProtectedVerifierClient()
    submission = build_submission(
        artifact_or_commit_identifier="0" * 40,
        evaluation_request_id="EVAL-1",
        required_contract_or_oracle_version="1.1",
    )
    outcome = client.submit(submission)
    killed = outcome.state is ProjectState.BLOCKED_EXTERNAL_ACCESS and outcome.result is None
    return KillReport(killed, f"state={outcome.state.value}, result={outcome.result}")


def _configured_client_still_refuses_a_bad_shape(_: dict[str, Any]) -> KillReport:
    """Even with an endpoint injected, a leaking response is FAILED_PROVENANCE."""

    def transport(config: VerifierEndpointConfig, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "evaluation_request_id": payload["evaluation_request_id"],
            "verdict": "PASS",
            "oracle_version": "1.0.0",
            "oracle_health": {"mutant_source": "if x: return True  # seeded"},
        }

    client = ProtectedVerifierClient(
        VerifierEndpointConfig(base_url="https://verifier.invalid"), transport
    )
    outcome = client.submit(
        build_submission(
            artifact_or_commit_identifier="0" * 40,
            evaluation_request_id="EVAL-1",
            required_contract_or_oracle_version="1.1",
        )
    )
    killed = outcome.state is TaskState.FAILED_PROVENANCE and outcome.result is None
    return KillReport(killed, f"state={outcome.state.value}, because={outcome.rejected_because}")


def _unverified_knowledge_promoted(_: dict[str, Any]) -> KillReport:
    """Promote raw agent output straight to trusted operational knowledge."""
    item = admit_agent_output(
        item_id="K-mutant",
        statement="the router retries zero times on the eval gateway",
        producer_alias="researcher-r17",
        producer_family="openai",
        claimed_tier=KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE,
    )
    entered_below_floor = item.tier is KnowledgeTier.T2_HYPOTHESIS
    try:
        promote(item, KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE)
    except PromotionRejected as exc:
        return KillReport(
            entered_below_floor,
            f"entry tier {item.tier.value}; promotion refused: {exc}",
        )
    return KillReport(False, "unverified agent output was promoted to a trusted tier")


def _lanes_drift_onto_different_commits(_: dict[str, Any]) -> KillReport:
    """GATE-D2-19 A3: a commit change between lanes invalidates the set."""
    binding = CandidateBinding(commit_sha="a" * 40)
    evaluation_set = EvaluationSet(evaluation_request_id="EVAL-1", binding=binding)
    evaluation_set.record(LaneRun(Lane.VISIBLE, "a" * 40, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.MUTANT, "a" * 40, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.HIDDEN, "b" * 40, Verdict.PASS))
    killed = evaluation_set.invalidated and evaluation_set.verdict() is Verdict.FAIL
    return KillReport(killed, f"invalidated={evaluation_set.invalidated_because}")


_TEST_MUTANT_SPECS = [
    ("weaken_visible_assertion_expected_value", _weaken_an_assertion),
    ("delete_a_visible_assertion", _delete_an_assertion),
]

_GOVERNANCE_MUTANT_SPECS = [
    ("gate_declares_model_judge_in_verdict_path", _gate_admits_a_model_judge),
    ("auto_merge_skips_one_of_the_thirteen_requirements", _auto_merge_skips_a_requirement),
    ("auto_merge_tolerates_protected_asset_access", _auto_merge_tolerates_protected_access),
    ("implementing_agent_merges_its_own_pull_request", _implementing_agent_self_merges),
    ("verifier_response_smuggles_a_holdout_case_body", _verifier_smuggles_holdout_content),
    ("verifier_response_carries_a_sixth_field", _verifier_returns_extra_field),
    ("client_invents_a_verifier_endpoint_instead_of_blocking", _client_invents_an_endpoint),
    ("configured_client_accepts_a_leaking_response", _configured_client_still_refuses_a_bad_shape),
    ("unverified_agent_output_promoted_to_trusted", _unverified_knowledge_promoted),
    ("evaluation_lanes_run_against_different_commits", _lanes_drift_onto_different_commits),
]


def test_mutants() -> list[Mutant]:
    return [
        Mutant(
            mutant_id=f"MUT-TEST-{index:02d}",
            mutant_class=MutantClass.TEST,
            target="acceptance/visible",
            declared_as=None,
            description=f"Visible assertion mutation: {name}.",
            run=run,
        )
        for index, (name, run) in enumerate(_TEST_MUTANT_SPECS, start=1)
    ]


def governance_mutants() -> list[Mutant]:
    return [
        Mutant(
            mutant_id=f"MUT-GOV-{index:02d}",
            mutant_class=MutantClass.WORKFLOW_GOVERNANCE,
            target="harness_governance",
            declared_as=None,
            description=f"Workflow/governance mutation: {name}.",
            run=run,
        )
        for index, (name, run) in enumerate(_GOVERNANCE_MUTANT_SPECS, start=1)
    ]
