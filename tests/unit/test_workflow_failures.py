"""Contract Section 10.6 -- classify before retrying."""

from __future__ import annotations

import sqlite3

import pytest

from governance.states import FailureClass
from workflows.failures import (
    NEVER_RETRY,
    ClassifiedFailure,
    RetryPolicy,
    classify,
    idempotency_key,
)
from workflows.state import MissingCheckpointFields


def test_the_twelve_classes_are_the_contract_list():
    assert {str(c) for c in FailureClass} == {
        "TRANSIENT_PROVIDER_FAILURE",
        "RATE_LIMIT",
        "MODEL_UNAVAILABLE",
        "WORKER_CONTEXT_LIMIT",
        "TOOL_FAILURE",
        "TEST_FAILURE",
        "WIRING_FAILURE",
        "CONTRACT_DRIFT",
        "HOLDOUT_FAILURE",
        "ORACLE_INVALID",
        "PROTECTED_ACCESS",
        "INFRASTRUCTURE_FAILURE",
    }


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ClassifiedFailure(FailureClass.CONTRACT_DRIFT, "x"), FailureClass.CONTRACT_DRIFT),
        (RuntimeError("429 Too Many Requests"), FailureClass.RATE_LIMIT),
        (RuntimeError("context window exceeded"), FailureClass.WORKER_CONTEXT_LIMIT),
        (RuntimeError("model unavailable at provider"), FailureClass.MODEL_UNAVAILABLE),
        (RuntimeError("503 service unavailable"), FailureClass.TRANSIENT_PROVIDER_FAILURE),
        (AssertionError("expected 1 got 2"), FailureClass.TEST_FAILURE),
        (RuntimeError("stale contract version"), FailureClass.CONTRACT_DRIFT),
        (RuntimeError("holdout suite disagreed"), FailureClass.HOLDOUT_FAILURE),
        (RuntimeError("oracle health degraded"), FailureClass.ORACLE_INVALID),
        (RuntimeError("protected identity store returned 401"), FailureClass.PROTECTED_ACCESS),
        (TimeoutError("timed out"), FailureClass.TRANSIENT_PROVIDER_FAILURE),
        (FileNotFoundError("no such tool"), FailureClass.TOOL_FAILURE),
        (sqlite3.OperationalError("database is locked"), FailureClass.INFRASTRUCTURE_FAILURE),
        (MissingCheckpointFields(["terminus_commit"], []), FailureClass.WIRING_FAILURE),
    ],
)
def test_classification(exc: BaseException, expected: FailureClass):
    assert classify(exc) is expected


def test_an_unrecognised_failure_is_not_assumed_retryable():
    """The safe default is "infrastructure", not "try again and hope"."""
    assert classify(Exception("something nobody has seen before")) is FailureClass.INFRASTRUCTURE_FAILURE


def test_contract_violations_are_never_retried():
    policy = RetryPolicy()
    for cls in NEVER_RETRY:
        decision = policy.decide(ClassifiedFailure(cls, "x"), attempt=1)
        assert decision.retry is False
        assert decision.attempts_remaining == 0


def test_transient_failures_are_retried_within_budget():
    policy = RetryPolicy(max_attempts=3)
    exc = ClassifiedFailure(FailureClass.RATE_LIMIT, "429")
    assert policy.decide(exc, attempt=1).retry is True
    assert policy.decide(exc, attempt=2).retry is True
    assert policy.decide(exc, attempt=3).retry is False


def test_rework_classes_are_not_retried_identically():
    policy = RetryPolicy()
    decision = policy.decide(AssertionError("test failed"), attempt=1)
    assert decision.failure_class is FailureClass.TEST_FAILURE
    assert decision.retry is False


def test_no_retry_decision_ever_escalates_to_an_owner_interrupt():
    """``autonomy-policy.yaml -> must_not_interrupt_for: retry_or_fallback_selection``."""
    policy = RetryPolicy()
    for cls in FailureClass:
        decision = policy.decide(ClassifiedFailure(cls, "x"), attempt=1)
        assert decision.owner_interrupt_required is False


def test_idempotency_key_is_stable_across_a_resume():
    """Section 10.6: a resumed run must re-derive the same key, not a new one."""
    args = {
        "work_unit_id": "WU-0001",
        "graph_id": "task_graph",
        "node": "execute_work_unit",
        "input_hashes": {"work_unit": "sha256:abc"},
    }
    assert idempotency_key(**args) == idempotency_key(**args)
    changed = {**args, "input_hashes": {"work_unit": "sha256:def"}}
    assert idempotency_key(**changed) != idempotency_key(**args)
