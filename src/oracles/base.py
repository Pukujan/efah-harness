"""Oracle base types.

Contract EFAH-CONTRACT-001 v1.1 Sections 17.3 and 17.4.

Two rules shape everything in this module:

1. **The verdict path is deterministic.** :meth:`DeterministicOracle.decide` is
   a pure function of its subject. No I/O, no clock, no model call. Anything
   that needs the world is resolved by the caller and handed in. That is what
   makes the structural proof in :mod:`oracles.no_judge` possible at all -- you
   cannot prove the absence of a model call in a function that is free to reach
   out and get one.
2. **``UNVERIFIABLE`` is not a soft pass.** An oracle that cannot decide says
   so and routes to the next hierarchy level. Defaulting to PASS launders an
   unknown into a green gate; defaulting to FAIL trains the system to route
   around the oracle.

Health is emitted with *every* result (Section 17.4), not on request. A result
without health is not a trusted oracle result, and :class:`OracleResult`
refuses to construct one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import CompiledObject, EvidenceTier, content_hash, utc_now
from governance.states import DriftFinding, TaskState, Verdict

#: A verdict path may name any typed failure the contract already defines.
#: Section 10.6 task states and Section 19.2 drift findings both appear as
#: ``failure_state`` in the oracle definitions -- ORACLE-003 KB-003 uses
#: ``STALE_CONTRACT_VERSION``, which is a drift finding, not a task state.
FailureState = TaskState | DriftFinding

#: Contract Section 17.3, strongest first. The index is the hierarchy level.
ORACLE_HIERARCHY: tuple[str, ...] = (
    "exact_deterministic_execution_or_state",
    "static_ast_type_policy_checker",
    "property_differential_or_metamorphic_test",
    "reference_implementation",
    "reproducible_empirical_benchmark",
    "calibrated_model_judge",
    "owner_adjudication",
)

#: Levels 1-5 decide gates on their own. 6 and 7 are subjective; a judge is
#: advisory until calibrated (model-policy.yaml judge_calibration.posture).
DETERMINISTIC_LEVELS = frozenset({1, 2, 3, 4, 5})

#: Contract Section 17.4. All eleven, or the oracle is not trusted.
MINTING_REQUIREMENTS: tuple[str, ...] = (
    "deterministic_verdict_path_with_no_hidden_model_call",
    "structural_proof_no_judge_participates",
    "independent_second_checker_comparison_where_feasible",
    "known_good_fixtures",
    "known_bad_fixtures",
    "gaming_probes",
    "mutants_that_it_kills",
    "honest_unverifiable_output",
    "pinned_checker_test_suite",
    "version_and_content_hash",
    "last_audit_date_and_health_emitted_with_every_result",
)


class OracleNotMinted(RuntimeError):
    """Raised when an oracle is used for a gate before it satisfies Section 17.4."""


class OracleHealth(BaseModel):
    """Health emitted alongside every result.

    ``declared_fields`` comes from the oracle definition's
    ``health_emitted_with_every_result`` list, and :meth:`as_declared` fails
    loudly rather than omitting a field the definition promised.
    """

    model_config = ConfigDict(extra="forbid")

    oracle_id: str
    oracle_version: str
    content_hash: str
    last_audit_date: str
    fixture_suite_result: str
    checker_test_suite_result: str = "NOT_RUN_IN_THIS_PROCESS"
    extra: dict[str, Any] = Field(default_factory=dict)

    def as_declared(self, declared_fields: list[str]) -> dict[str, Any]:
        flat: dict[str, Any] = {
            "oracle_id": self.oracle_id,
            "oracle_version": self.oracle_version,
            "content_hash": self.content_hash,
            "last_audit_date": self.last_audit_date,
            "fixture_suite_result": self.fixture_suite_result,
            "checker_test_suite_result": self.checker_test_suite_result,
            **self.extra,
        }
        missing = [f for f in declared_fields if f not in flat]
        if missing:
            raise OracleNotMinted(
                f"{self.oracle_id} promised health fields it did not emit: {missing}"
            )
        return {f: flat[f] for f in declared_fields}


class OracleResult(BaseModel):
    """One oracle verdict, with the provenance that makes it usable as evidence."""

    model_config = ConfigDict(extra="forbid")

    oracle_id: str
    oracle_version: str
    hierarchy_level: int
    verdict: Verdict
    failure_state: FailureState | None = None
    reasons: list[str] = Field(default_factory=list)
    subject_ref: str | None = None
    candidate_commit: str | None = None
    health: dict[str, Any]
    evidence_tier: EvidenceTier = EvidenceTier.DETERMINISTIC_ORACLE
    decided_at: str = Field(default_factory=utc_now)
    second_checker_agreed: bool | None = None

    def to_compiled_object(self, *, created_by_alias: str = "oracle-o02") -> CompiledObject:
        """Persistable form. Section 8: every compiled object carries an envelope."""
        return CompiledObject.create(
            schema_id="efah.oracle_result",
            created_by_alias=created_by_alias,
            body=self.model_dump(mode="json"),
        )


class Decision(BaseModel):
    """The pure output of a verdict path, before health or provenance is attached."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    reasons: list[str] = Field(default_factory=list)
    failure_state: FailureState | None = None
    second_checker_agreed: bool | None = None
    health_extra: dict[str, Any] = Field(default_factory=dict)


def unverifiable(reason: str, **health_extra: Any) -> Decision:
    return Decision(verdict=Verdict.UNVERIFIABLE, reasons=[reason], health_extra=health_extra)


def fail(reasons: list[str], failure_state: FailureState, **health_extra: Any) -> Decision:
    return Decision(
        verdict=Verdict.FAIL,
        reasons=reasons,
        failure_state=failure_state,
        health_extra=health_extra,
    )


def passed(reasons: list[str] | None = None, **health_extra: Any) -> Decision:
    return Decision(verdict=Verdict.PASS, reasons=reasons or [], health_extra=health_extra)


class DeterministicOracle(ABC):
    """A Section 17.4 trusted oracle.

    Subclasses implement :meth:`decide` only. Everything else -- health,
    provenance, the definition contract -- is handled here so a new oracle
    cannot forget it.
    """

    #: Populated from the pack's oracle definition YAML.
    definition: dict[str, Any]

    def __init__(self, definition: dict[str, Any], *, minted: dict[str, Any] | None = None) -> None:
        self.definition = definition
        self._minted = minted or {}
        declared_id = definition.get("oracle_id")
        if declared_id != self.oracle_id:
            raise OracleNotMinted(
                f"{type(self).__name__} implements {self.oracle_id} but was given "
                f"definition {declared_id!r}"
            )
        if definition.get("model_call_in_verdict_path") is not False:
            raise OracleNotMinted(f"{declared_id}: definition does not declare a model-free path")
        if definition.get("judge_participates") is not False:
            raise OracleNotMinted(f"{declared_id}: definition allows a judge in the verdict path")

    # --- identity -------------------------------------------------------
    @property
    @abstractmethod
    def oracle_id(self) -> str: ...

    @property
    def oracle_version(self) -> str:
        return str(self.definition["oracle_version"])

    @property
    def hierarchy_level(self) -> int:
        return int(self.definition["hierarchy_level"])

    @property
    def declared_health_fields(self) -> list[str]:
        return list(self.definition.get("health_emitted_with_every_result", []))

    @property
    def pinned_checker_test_suite(self) -> str:
        return str(self.definition["pinned_checker_test_suite"])

    # --- verdict path ---------------------------------------------------
    @abstractmethod
    def decide(self, subject: Any) -> Decision:
        """Pure verdict path. No I/O, no clock, no model call."""

    # --- result assembly ------------------------------------------------
    def health(self, *, fixture_suite_result: str, extra: dict[str, Any]) -> OracleHealth:
        record = self._minted
        return OracleHealth(
            oracle_id=self.oracle_id,
            oracle_version=self.oracle_version,
            content_hash=record.get("content_hash", content_hash(self.definition)),
            last_audit_date=record.get("last_audit_date", "NOT_MINTED"),
            fixture_suite_result=fixture_suite_result,
            checker_test_suite_result=record.get("checker_test_suite_result", "NOT_RUN_IN_THIS_PROCESS"),
            extra=extra,
        )

    def evaluate(
        self,
        subject: Any,
        *,
        subject_ref: str | None = None,
        candidate_commit: str | None = None,
        fixture_suite_result: str = "NOT_RUN_IN_THIS_PROCESS",
    ) -> OracleResult:
        decision = self.decide(subject)
        health = self.health(
            fixture_suite_result=fixture_suite_result, extra=decision.health_extra
        )
        return OracleResult(
            oracle_id=self.oracle_id,
            oracle_version=self.oracle_version,
            hierarchy_level=self.hierarchy_level,
            verdict=decision.verdict,
            failure_state=decision.failure_state,
            reasons=decision.reasons,
            subject_ref=subject_ref,
            candidate_commit=candidate_commit,
            health=health.as_declared(self.declared_health_fields),
            second_checker_agreed=decision.second_checker_agreed,
        )


def is_placeholder(value: Any) -> bool:
    """Contract Section 5.2 / ORACLE-001 GP-003.

    A wiring field that says ``TODO`` proves nothing. Neither does ``true``
    standing in for a health check. Both are the failure this catches.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    text = str(value).strip()
    if not text:
        return True
    lowered = text.lower()
    markers = ("todo", "tbd", "placeholder", "stub", "fixme", "n/a", "none", "...", "true", "false")
    return lowered in markers or lowered.startswith(("todo", "tbd_", "placeholder", "stub"))
