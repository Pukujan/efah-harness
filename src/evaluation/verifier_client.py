"""Protected verifier client — the build side of the isolation seam.

Contract Section 17.2. This module is the *only* place the candidate system
talks to the protected verifier, and it is deliberately small enough to audit
in one sitting.

Three properties matter, in this order:

1. **The submission shape is closed.** Exactly four fields go out
   (``artifact_or_commit_identifier``, ``allowed_runtime_inputs``,
   ``evaluation_request_id``, ``required_contract_or_oracle_version``).
   ``extra="forbid"`` means a fifth cannot be added by accident, and
   :func:`build_submission` is the only constructor.
2. **The response shape is closed, and content is inspected, not trusted.**
   Exactly five fields come back. A response carrying hidden assertion text,
   private fixture content, mutant source, or a holdout case body is rejected
   as ``FAILED_PROVENANCE`` -- GATE-D1-08 A5. Field-shape validation alone is
   not enough: ``oracle_health`` is a free-form mapping, and that is precisely
   where leaked content would ride.
3. **There is no endpoint here, and there must not be one.** The endpoint comes
   from injected configuration that is absent by default. With no configuration
   the client returns ``BLOCKED_EXTERNAL_ACCESS`` and stops. It does not guess a
   URL, read one from the pack, or fall back to a local implementation --
   inventing a route is the failure GATE-D1-08 forbids, and a local fallback
   would make the verifier circular, which is worse.

``repositories.yaml`` records the sealed side's URL as ``not_supplied_to_builder``
and that is the correct, permanent state of this file. A 404 is the right
answer, not a problem to solve.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from governance.envelope import CompiledObject
from governance.states import FailureClass, ProjectState, TaskState, Verdict

INTERFACE_VERSION = "1.0.0"

#: Contract Section 17.2 / repositories.yaml permitted_submission_fields.
PERMITTED_SUBMISSION_FIELDS: tuple[str, ...] = (
    "artifact_or_commit_identifier",
    "allowed_runtime_inputs",
    "evaluation_request_id",
    "required_contract_or_oracle_version",
)

#: Contract Section 17.2 / repositories.yaml permitted_response_shape.
PERMITTED_RESPONSE_FIELDS: tuple[str, ...] = (
    "evaluation_request_id",
    "verdict",
    "oracle_version",
    "oracle_health",
    "failure_class",
)

#: ``oracle_health`` is the only open mapping in the approved response shape,
#: which makes it the only place holdout content could ride across the seam. It
#: is therefore closed by allowlist rather than policed by pattern: these are
#: the health fields the pack's oracle definitions declare, plus the generic
#: liveness fields a sealed implementation may reasonably add. A key outside
#: this set is rejected without needing to guess what it contains.
PERMITTED_HEALTH_KEYS: frozenset[str] = frozenset(
    {
        "oracle_id",
        "oracle_version",
        "content_hash",
        "last_audit_date",
        "fixture_suite_result",
        "checker_test_suite_result",
        "clock_skew_observed",
        "unresolvable_reference_count",
        "health_status",
        "degraded",
        "last_run_at",
    }
)

#: GATE-D1-08 A5 forbidden_content, as detectable markers in any string value.
LEAK_VALUE_PATTERN = re.compile(
    r"(\bdef\s+\w+\s*\(|\bclass\s+\w+\s*[:(]|\bassert\s|```|@@ -\d|<<<<<<<|\bimport\s+\w+)"
)
#: Health is a handful of scalars. Anything long is carrying something else.
MAX_HEALTH_VALUE_CHARS = 512


class SubmissionShapeViolation(RuntimeError):
    """An attempt to send the verifier something Section 17.2 does not permit."""


class VerifierSubmission(BaseModel):
    """The only thing the candidate may send. Four fields, no more."""

    model_config = ConfigDict(extra="forbid")

    artifact_or_commit_identifier: str
    allowed_runtime_inputs: dict[str, str] = Field(default_factory=dict)
    evaluation_request_id: str
    required_contract_or_oracle_version: str


class VerifierResult(BaseModel):
    """The only thing the verifier may return. Five fields, no more."""

    model_config = ConfigDict(extra="forbid")

    evaluation_request_id: str
    verdict: Verdict
    oracle_version: str
    oracle_health: dict[str, Any] = Field(default_factory=dict)
    failure_class: FailureClass | None = None


@dataclass(frozen=True)
class VerifierEndpointConfig:
    """Injected, never defaulted. Absent configuration is the normal state."""

    base_url: str
    submit_path: str = "/evaluations"
    timeout_seconds: float = 120.0
    #: DEC-002: gate-bearing evidence never retries silently.
    max_retries: int = 0


class VerifierTransport(Protocol):
    """Whatever actually carries the request. Injected so nothing here dials out."""

    def __call__(
        self, config: VerifierEndpointConfig, payload: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclass
class VerifierOutcome:
    """What the build side is allowed to know after a submission."""

    evaluation_request_id: str
    state: ProjectState | TaskState
    result: VerifierResult | None = None
    rejected_because: list[str] | None = None
    submission: VerifierSubmission | None = None

    @property
    def accepted(self) -> bool:
        return self.result is not None and not self.rejected_because

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "schema_assert_on_verdict_payload",
            "expected": "no_forbidden_field_present",
            "interface_version": INTERFACE_VERSION,
            "evaluation_request_id": self.evaluation_request_id,
            "state": self.state.value,
            "permitted_submission_fields": list(PERMITTED_SUBMISSION_FIELDS),
            "permitted_response_fields": list(PERMITTED_RESPONSE_FIELDS),
            "submission_fields_sent": (
                sorted(self.submission.model_dump().keys()) if self.submission else []
            ),
            "result": self.result.model_dump(mode="json") if self.result else None,
            "rejected_because": self.rejected_because or [],
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.verifier_outcome",
            created_by_alias="release-v04",
            body=self.as_evidence(),
        )


def build_submission(
    *,
    artifact_or_commit_identifier: str,
    evaluation_request_id: str,
    required_contract_or_oracle_version: str,
    allowed_runtime_inputs: dict[str, str] | None = None,
) -> VerifierSubmission:
    """The only constructor. Keyword-only, so nothing is passed positionally by mistake."""
    return VerifierSubmission(
        artifact_or_commit_identifier=artifact_or_commit_identifier,
        evaluation_request_id=evaluation_request_id,
        required_contract_or_oracle_version=required_contract_or_oracle_version,
        allowed_runtime_inputs=allowed_runtime_inputs or {},
    )


def scan_for_leaked_content(payload: dict[str, Any]) -> list[str]:
    """GATE-D1-08 A5. Shape validation is necessary but not sufficient.

    ``oracle_health`` is closed by allowlist and restricted to scalars, so a
    sealed implementation cannot return hidden assertion text, private fixture
    content, mutant source, or a holdout case body while still satisfying the
    five-field schema.
    """
    findings: list[str] = []
    health = payload.get("oracle_health")
    if health is not None and not isinstance(health, dict):
        findings.append("oracle_health is not a mapping of scalar health fields")
        health = {}
    for key, value in (health or {}).items():
        if key not in PERMITTED_HEALTH_KEYS:
            findings.append(
                f"oracle_health.{key}: key is outside the approved health field set; the seam "
                "carries health, not oracle internals"
            )
            continue
        if isinstance(value, (dict, list, tuple)):
            findings.append(
                f"oracle_health.{key}: nested structure where a scalar is required"
            )
            continue
        if isinstance(value, str):
            if LEAK_VALUE_PATTERN.search(value):
                findings.append(
                    f"oracle_health.{key}: value contains source, diff, or assertion text"
                )
            elif len(value) > MAX_HEALTH_VALUE_CHARS:
                findings.append(
                    f"oracle_health.{key}: value is {len(value)} chars; health carries scalars"
                )

    for field_name in ("evaluation_request_id", "oracle_version"):
        value = payload.get(field_name)
        if isinstance(value, str) and LEAK_VALUE_PATTERN.search(value):
            findings.append(f"{field_name}: contains source or assertion text")
        elif isinstance(value, str) and len(value) > MAX_HEALTH_VALUE_CHARS:
            findings.append(f"{field_name}: value is {len(value)} chars; identifiers are short")

    failure_class = payload.get("failure_class")
    if isinstance(failure_class, str) and failure_class not in {f.value for f in FailureClass}:
        findings.append(
            f"failure_class {failure_class!r} is not a typed class; only typed classes may "
            "cross the seam"
        )
    return findings


def validate_response(payload: dict[str, Any]) -> tuple[VerifierResult | None, list[str]]:
    """Enforce the shape, then inspect the content. Both, in that order."""
    findings: list[str] = []
    extra = sorted(set(payload) - set(PERMITTED_RESPONSE_FIELDS))
    if extra:
        findings.append(f"response carries fields outside the contract-approved shape: {extra}")
    findings.extend(scan_for_leaked_content(payload))

    result: VerifierResult | None = None
    try:
        result = VerifierResult.model_validate(payload)
    except ValidationError as exc:
        findings.append(f"response does not validate against the approved shape: {exc.errors()}")

    if findings:
        return None, findings
    return result, []


class ProtectedVerifierClient:
    """Submits a candidate commit and receives a verdict shape. Nothing else."""

    def __init__(
        self,
        config: VerifierEndpointConfig | None = None,
        transport: VerifierTransport | Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        #: Absent by default, and absent is correct on the build side.
        self._config = config
        self._transport = transport

    @property
    def configured(self) -> bool:
        return self._config is not None

    def submit(self, submission: VerifierSubmission) -> VerifierOutcome:
        if not isinstance(submission, VerifierSubmission):
            raise SubmissionShapeViolation(
                "only a VerifierSubmission may cross the seam; build it with build_submission()"
            )

        if self._config is None:
            # Typed blocker, not an invented route. Contract Section 6.2.
            return VerifierOutcome(
                evaluation_request_id=submission.evaluation_request_id,
                state=ProjectState.BLOCKED_EXTERNAL_ACCESS,
                rejected_because=[
                    (
                        "no protected-verifier endpoint is configured on the build side, "
                        "and none may be; the sealed side is reached only through injected "
                        "configuration held by the verifier service identity"
                    )
                ],
                submission=submission,
            )
        if self._transport is None:
            return VerifierOutcome(
                evaluation_request_id=submission.evaluation_request_id,
                state=ProjectState.BLOCKED_EXTERNAL_ACCESS,
                rejected_because=["endpoint configured but no transport injected"],
                submission=submission,
            )

        payload = self._transport(self._config, submission.model_dump(mode="json"))
        if not isinstance(payload, dict):
            return VerifierOutcome(
                evaluation_request_id=submission.evaluation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=[f"verifier returned {type(payload).__name__}, not a result shape"],
                submission=submission,
            )

        result, findings = validate_response(payload)
        if findings:
            return VerifierOutcome(
                evaluation_request_id=submission.evaluation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=findings,
                submission=submission,
            )
        assert result is not None
        if result.evaluation_request_id != submission.evaluation_request_id:
            return VerifierOutcome(
                evaluation_request_id=submission.evaluation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=[
                    (
                        f"result is for {result.evaluation_request_id!r}, "
                        "not the submitted request"
                    )
                ],
                submission=submission,
            )
        return VerifierOutcome(
            evaluation_request_id=submission.evaluation_request_id,
            state=TaskState.PASSED if result.verdict is Verdict.PASS else TaskState.VERIFYING,
            result=result,
            submission=submission,
        )
