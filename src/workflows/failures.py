"""Failure classification and retry posture -- Contract Section 10.6.

    The runtime MUST distinguish [twelve failure classes]. All external side
    effects MUST be idempotent or protected by idempotency keys.

Classification happens *before* the retry decision, not after, because the
classes disagree about what a retry means. Retrying a ``RATE_LIMIT`` is correct;
retrying a ``CONTRACT_DRIFT`` re-runs work the contract has already invalidated,
and retrying a ``PROTECTED_ACCESS`` repeats an access that must never have
happened once.

``autonomy-policy.yaml`` is equally clear about the other direction: a retry or
fallback decision must never become an owner interrupt. Nothing here escalates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from governance.envelope import content_hash
from governance.states import FailureClass

#: Classes where the same work, run again, can legitimately succeed.
RETRYABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.TRANSIENT_PROVIDER_FAILURE,
        FailureClass.RATE_LIMIT,
        FailureClass.MODEL_UNAVAILABLE,
        FailureClass.TOOL_FAILURE,
        FailureClass.INFRASTRUCTURE_FAILURE,
    }
)

#: Classes that need a different input, not another attempt.
REWORK_REQUIRED: frozenset[FailureClass] = frozenset(
    {
        FailureClass.WORKER_CONTEXT_LIMIT,
        FailureClass.TEST_FAILURE,
        FailureClass.WIRING_FAILURE,
        FailureClass.HOLDOUT_FAILURE,
    }
)

#: Classes where retrying is itself a contract violation.
NEVER_RETRY: frozenset[FailureClass] = frozenset(
    {
        FailureClass.CONTRACT_DRIFT,
        FailureClass.ORACLE_INVALID,
        FailureClass.PROTECTED_ACCESS,
    }
)

_MESSAGE_SIGNALS: tuple[tuple[tuple[str, ...], FailureClass], ...] = (
    (("rate limit", "429", "too many requests"), FailureClass.RATE_LIMIT),
    (("context length", "context window", "too many tokens"), FailureClass.WORKER_CONTEXT_LIMIT),
    (("model not found", "model unavailable", "no such model", "404 model"), FailureClass.MODEL_UNAVAILABLE),
    (("503", "502", "504", "bad gateway", "service unavailable", "overloaded"), FailureClass.TRANSIENT_PROVIDER_FAILURE),
    (("assertion", "test failed", "pytest"), FailureClass.TEST_FAILURE),
    (("not wired", "unreachable node", "missing edge", "no path"), FailureClass.WIRING_FAILURE),
    (("contract version", "contract drift", "stale contract"), FailureClass.CONTRACT_DRIFT),
    (("holdout",), FailureClass.HOLDOUT_FAILURE),
    (("oracle",), FailureClass.ORACLE_INVALID),
    (("protected", "6364", "401"), FailureClass.PROTECTED_ACCESS),
)

_EXCEPTION_SIGNALS: tuple[tuple[tuple[str, ...], FailureClass], ...] = (
    (("TimeoutError", "ConnectionError", "ConnectTimeout", "ReadTimeout"), FailureClass.TRANSIENT_PROVIDER_FAILURE),
    (("AssertionError",), FailureClass.TEST_FAILURE),
    (("MissingRequiredCredential",), FailureClass.INFRASTRUCTURE_FAILURE),
    (("MissingCheckpointFields",), FailureClass.WIRING_FAILURE),
    # Ordered before the generic OSError entry: FileNotFoundError is an OSError
    # subclass, and "the tool is not there" is a tool failure, not a sick host.
    (("FileNotFoundError", "PermissionError"), FailureClass.TOOL_FAILURE),
    (("OSError", "IOError", "sqlite3.OperationalError", "DatabaseError"), FailureClass.INFRASTRUCTURE_FAILURE),
)


class ClassifiedFailure(Exception):
    """A failure that already knows its Section 10.6 class.

    Raising this instead of a bare ``RuntimeError`` means the classifier never
    has to guess, and the guess is the part that goes wrong.
    """

    def __init__(self, failure_class: FailureClass, message: str) -> None:
        self.failure_class = failure_class
        super().__init__(message)


def classify(exc: BaseException) -> FailureClass:
    """Map an exception onto Section 10.6's closed set.

    The default is ``INFRASTRUCTURE_FAILURE`` rather than a cheerful retryable
    class: an unrecognised failure is not evidence that trying again will work.
    """
    if isinstance(exc, ClassifiedFailure):
        return exc.failure_class

    names = {type(exc).__name__} | {c.__name__ for c in type(exc).__mro__}
    qualified = f"{type(exc).__module__}.{type(exc).__name__}"
    for signals, cls in _EXCEPTION_SIGNALS:
        if any(sig in names or sig == qualified for sig in signals):
            return cls

    text = str(exc).lower()
    for signals, cls in _MESSAGE_SIGNALS:
        if any(sig in text for sig in signals):
            return cls

    return FailureClass.INFRASTRUCTURE_FAILURE


@dataclass(frozen=True)
class RetryDecision:
    """Why the runtime will or will not run this node again."""

    failure_class: FailureClass
    retry: bool
    attempts_remaining: int
    rationale: str
    #: Section 10.7: a retry decision is never an owner interrupt.
    owner_interrupt_required: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries. Section 10.6 wants recovery, not an infinite loop."""

    max_attempts: int = 3

    def decide(self, exc: BaseException, *, attempt: int) -> RetryDecision:
        cls = classify(exc)
        remaining = max(self.max_attempts - attempt, 0)
        if cls in NEVER_RETRY:
            return RetryDecision(cls, False, 0, f"{cls} must not be retried; escalate as a typed blocker")
        if cls in REWORK_REQUIRED:
            return RetryDecision(cls, False, remaining, f"{cls} needs different input, not another attempt")
        if cls in RETRYABLE and remaining > 0:
            return RetryDecision(cls, True, remaining, f"{cls} is transient; {remaining} attempt(s) left")
        return RetryDecision(cls, False, remaining, f"{cls} exhausted its retry budget")


def idempotency_key(
    *,
    work_unit_id: str,
    graph_id: str,
    node: str,
    input_hashes: dict[str, str] | Any,
) -> str:
    """Section 10.6: external side effects need an idempotency key.

    Derived from the inputs, so a resumed run re-derives the *same* key and the
    external system deduplicates. A random key would defeat the purpose.
    """
    return content_hash(
        {
            "work_unit_id": work_unit_id,
            "graph_id": graph_id,
            "node": node,
            "input_hashes": input_hashes,
        }
    )
