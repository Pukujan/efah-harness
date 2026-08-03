"""The protected-verifier seam. GATE-D1-08 · contract Section 17.2.

Two things are under test and they pull in opposite directions:

* the seam must **work** -- a correctly shaped response is accepted;
* the seam must **refuse** -- anything else is ``FAILED_PROVENANCE``, and an
  unconfigured client blocks rather than inventing a route.

Nothing here reaches the network, and nothing here names the sealed repository.
A2 of the same gate forbids a build-side file from containing it, and a test is
a build-side file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from evaluation.binding import CandidateBinding
from evaluation.verifier_client import (
    INTERFACE_VERSION,
    PERMITTED_RESPONSE_FIELDS,
    PERMITTED_SUBMISSION_FIELDS,
    ProtectedVerifierClient,
    SubmissionShapeViolation,
    VerifierEndpointConfig,
    VerifierSubmission,
    build_submission,
    validate_response,
)
from governance.states import ProjectState, TaskState, Verdict
from holdouts.suite import HoldoutLane

INTERFACE_DIR = Path(__file__).resolve().parents[2] / "verifier-interface"


def _good_payload(request_id: str = "EVAL-1") -> dict:
    return {
        "evaluation_request_id": request_id,
        "verdict": "PASS",
        "oracle_version": "1.0.0",
        "oracle_health": {
            "oracle_id": "ORACLE-003",
            "content_hash": "sha256:" + "0" * 64,
            "last_audit_date": "2026-08-02",
            "fixture_suite_result": "PASS",
        },
        "failure_class": None,
    }


# --- the submission shape -------------------------------------------------

def test_only_the_four_permitted_submission_fields_exist():
    submission = build_submission(
        artifact_or_commit_identifier="a" * 40,
        evaluation_request_id="EVAL-1",
        required_contract_or_oracle_version="1.1",
    )
    assert sorted(submission.model_dump()) == sorted(PERMITTED_SUBMISSION_FIELDS)


def test_a_fifth_submission_field_is_rejected():
    with pytest.raises(ValidationError):
        VerifierSubmission(
            artifact_or_commit_identifier="a" * 40,
            evaluation_request_id="EVAL-1",
            required_contract_or_oracle_version="1.1",
            hint="the holdout expects 42",
        )


def test_only_a_verifier_submission_may_cross_the_seam():
    client = ProtectedVerifierClient(VerifierEndpointConfig(base_url="https://x.invalid"))
    with pytest.raises(SubmissionShapeViolation):
        client.submit({"artifact_or_commit_identifier": "a" * 40})  # type: ignore[arg-type]


# --- the response shape ---------------------------------------------------

def test_a_contract_approved_response_is_accepted():
    result, findings = validate_response(_good_payload())
    assert findings == []
    assert result is not None
    assert result.verdict is Verdict.PASS
    assert sorted(result.model_dump()) == sorted(PERMITTED_RESPONSE_FIELDS)


@pytest.mark.parametrize(
    "forbidden_class,payload_patch",
    [
        ("hidden_assertion_text", {"hidden_assertions": ["reachability from an entry point"]}),
        ("private_fixture_content", {"private_fixture": {"input": 7, "expect": 42}}),
        ("mutant_source", {"mutant_source": "def mutated(x): return True"}),
        ("holdout_case_body", {"holdout_case_body": "assert compute(7) == 42"}),
    ],
)
def test_forbidden_content_as_an_extra_field_is_rejected(forbidden_class, payload_patch):
    payload = _good_payload() | payload_patch
    result, findings = validate_response(payload)
    assert result is None, forbidden_class
    assert findings


@pytest.mark.parametrize(
    "health_patch",
    [
        {"holdout_case": "assert compute(7) == 42"},
        {"mutant_source": "def mutated(x):\n    return True\n"},
        {"hidden_assertion": "module reachable from an approved entry point"},
        {"health_status": "def leaked(x):\n    return x\n"},
        {"health_status": "x" * 900},
        {"fixture_suite_result": ["PASS", "case body here"]},
    ],
)
def test_forbidden_content_smuggled_through_oracle_health_is_rejected(health_patch):
    """The shape is satisfied; the content is not. Shape alone is insufficient."""
    payload = _good_payload()
    payload["oracle_health"] = payload["oracle_health"] | health_patch
    result, findings = validate_response(payload)
    assert result is None
    assert findings


def test_an_untyped_failure_class_is_rejected():
    payload = _good_payload() | {"failure_class": "the third assertion about leases failed"}
    result, findings = validate_response(payload)
    assert result is None
    assert findings


def test_a_leaking_response_reaches_the_client_as_failed_provenance():
    def transport(config, payload):
        return _good_payload(payload["evaluation_request_id"]) | {
            "holdout_case_body": "assert compute(7) == 42"
        }

    client = ProtectedVerifierClient(
        VerifierEndpointConfig(base_url="https://verifier.invalid"), transport
    )
    outcome = client.submit(
        build_submission(
            artifact_or_commit_identifier="a" * 40,
            evaluation_request_id="EVAL-1",
            required_contract_or_oracle_version="1.1",
        )
    )
    assert outcome.state is TaskState.FAILED_PROVENANCE
    assert outcome.result is None
    assert outcome.rejected_because


def test_a_result_for_a_different_request_is_rejected():
    def transport(config, payload):
        return _good_payload("EVAL-SOMEONE-ELSE")

    client = ProtectedVerifierClient(
        VerifierEndpointConfig(base_url="https://verifier.invalid"), transport
    )
    outcome = client.submit(
        build_submission(
            artifact_or_commit_identifier="a" * 40,
            evaluation_request_id="EVAL-1",
            required_contract_or_oracle_version="1.1",
        )
    )
    assert outcome.state is TaskState.FAILED_PROVENANCE


# --- the endpoint is absent, and that is correct --------------------------

def test_an_unconfigured_client_blocks_rather_than_inventing_a_route():
    client = ProtectedVerifierClient()
    assert client.configured is False
    outcome = client.submit(
        build_submission(
            artifact_or_commit_identifier="a" * 40,
            evaluation_request_id="EVAL-1",
            required_contract_or_oracle_version="1.1",
        )
    )
    assert outcome.state is ProjectState.BLOCKED_EXTERNAL_ACCESS
    assert outcome.result is None
    assert "no protected-verifier endpoint is configured" in outcome.rejected_because[0]


def test_the_hidden_lane_is_unverifiable_not_passing_when_the_seam_is_closed():
    """A holdout lane that cannot run must not report a green."""
    result = HoldoutLane().run(
        CandidateBinding(commit_sha="a" * 40), evaluation_request_id="EVAL-1"
    )
    assert result.lane_run.verdict is Verdict.UNVERIFIABLE
    assert result.as_evidence()["holdout_content_present_on_build_side"] is False
    assert result.blocked_reason


def test_no_endpoint_literal_lives_in_the_client_module():
    source = (
        Path(__file__).resolve().parents[2] / "src" / "evaluation" / "verifier_client.py"
    ).read_text()
    for line in source.splitlines():
        if line.lstrip().startswith("#") or '"""' in line:
            continue
        assert "https://" not in line, line


# --- the published interface ----------------------------------------------

def test_the_published_schemas_match_the_client(tmp_path):
    submission_schema = json.loads(
        (INTERFACE_DIR / "schema" / "v1" / "submission.schema.json").read_text()
    )
    result_schema = json.loads(
        (INTERFACE_DIR / "schema" / "v1" / "result.schema.json").read_text()
    )
    assert sorted(submission_schema["properties"]) == sorted(PERMITTED_SUBMISSION_FIELDS)
    assert sorted(result_schema["properties"]) == sorted(PERMITTED_RESPONSE_FIELDS)
    assert submission_schema["additionalProperties"] is False
    assert result_schema["additionalProperties"] is False


def test_the_interface_declaration_agrees_with_the_code():
    declared = yaml.safe_load((INTERFACE_DIR / "interface-v1.yaml").read_text())
    assert declared["interface_version"] == INTERFACE_VERSION
    assert (INTERFACE_DIR / "VERSION").read_text().strip() == INTERFACE_VERSION
    assert sorted(declared["submission"]["permitted_fields"]) == sorted(
        PERMITTED_SUBMISSION_FIELDS
    )
    assert sorted(declared["result"]["permitted_fields"]) == sorted(PERMITTED_RESPONSE_FIELDS)
    assert declared["isolation"]["endpoint_configured_on_build_side"] is False
    assert declared["isolation"]["build_side_local_fallback"] == "forbidden"
    assert (
        declared["isolation"]["remediation_must_not_include"]
        == "granting_builder_access_to_sealed_side"
    )
