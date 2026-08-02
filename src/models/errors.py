"""Typed failures for the model router, gateways, and worker adapters.

Every failure here maps onto an existing enum from :mod:`governance.states`.
Contract Section 9.4 and Section 10.6: a failure without a typed class is not a
reportable failure, and inventing a new state string is drift. Nothing in this
module defines a new state -- it only binds an exception to one that already
exists.
"""

from __future__ import annotations

from governance.states import DriftFinding, FailureClass, TaskState


class ModelPolicyError(RuntimeError):
    """Base class. Carries the typed state the harness must report."""

    #: Set by subclasses to a member of an existing closed enumeration.
    typed_state: str = ""

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        self.detail = detail
        super().__init__(f"{self.typed_state}: {message}" if self.typed_state else message)


class FailedProvenanceError(ModelPolicyError):
    """DEC-002 violation: gate-bearing traffic on the wrong gateway, an eval
    client that can retry, or an eval call made without a valid preflight.

    All three produce a *green result with a false provenance record*, which is
    exactly what contract Section 18 exists to prevent.
    """

    typed_state = TaskState.FAILED_PROVENANCE


class RoleConflictError(ModelPolicyError):
    """Contract Section 12.2 / model-policy ``role_incompatibilities``."""

    typed_state = DriftFinding.ROLE_CONFLICT


class BlindingViolationError(RoleConflictError):
    """GATE-D1-06: a task-facing payload carried a real vendor or model identity."""


class FailedOracleError(ModelPolicyError):
    """``request_policy.violation_state`` -- a request configured so that a
    capable model looks incapable (``max_tokens`` below the tool-call floor).
    A false negative recorded as evidence is a broken oracle, not a bad answer.
    """

    typed_state = TaskState.FAILED_ORACLE


class ProhibitedModelError(ModelPolicyError):
    """``prohibited_models`` is evidence-backed; the router may not override it."""

    typed_state = DriftFinding.UNAPPROVED_SCOPE_EXPANSION


class ModelUnavailableError(ModelPolicyError):
    """No alias satisfies the request under the current empirical availability."""

    typed_state = FailureClass.MODEL_UNAVAILABLE


class AvailabilityProbeRequiredError(ModelPolicyError):
    """``availability_probe.required_before_first_dispatch`` is unsatisfied.

    A static assumption of availability is not evidence -- the whole flat-rate
    ``[aws]`` lane was up in the morning and 503 by the afternoon of the same day.
    """

    typed_state = FailureClass.MODEL_UNAVAILABLE


class RateLimitError(ModelPolicyError):
    """A 429 that reached us. Recorded as evidence before any harness retry."""

    typed_state = FailureClass.RATE_LIMIT


class TransientProviderError(ModelPolicyError):
    typed_state = FailureClass.TRANSIENT_PROVIDER_FAILURE


class GatewayRequestError(ModelPolicyError):
    """A gateway answered with a client error we cannot classify more precisely.

    Deliberately not swallowed: on the eval path an unexpected 4xx is exactly
    the loud failure ``drop_params: false`` exists to produce.
    """

    typed_state = FailureClass.INFRASTRUCTURE_FAILURE


class ProtectedAccessError(ModelPolicyError):
    """An unprivileged caller asked for a real model identity (Section 11.2)."""

    typed_state = FailureClass.PROTECTED_ACCESS


class SessionReuseError(ModelPolicyError):
    """Contract Section 10.5: sessions are fresh per invocation. Reuse would
    carry conversational memory that the contract prohibits by default.
    """

    typed_state = FailureClass.CONTRACT_DRIFT


class AdapterUnavailableError(ModelPolicyError):
    """An optional worker adapter was requested but is not usable here."""

    typed_state = FailureClass.INFRASTRUCTURE_FAILURE
