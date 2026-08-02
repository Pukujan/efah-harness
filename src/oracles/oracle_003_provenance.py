"""ORACLE-003 — Provenance binding (hierarchy level 2).

Implements ``project-pack/acceptance/oracle-definitions/ORACLE-003-provenance-binding.yaml``
exactly. Contract Sections 8, 18, 23. It is the mechanical form of the
contract's shortest sentence: *"Done" without named evidence is invalid.*

Note what the definition deliberately does **not** allow. ``unverifiable_when``
lists exactly two conditions, both of them service outages. A missing hash is a
FAIL, not an UNVERIFIABLE -- absence of provenance is a determinate answer, and
treating it as an unknown would be the same laundering the UNVERIFIABLE rule
exists to prevent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import CompiledObject, EvidenceTier, content_hash
from governance.states import DriftFinding, TaskState
from oracles.base import Decision, DeterministicOracle, fail, passed, unverifiable

#: GATE-D1-02 A1 — the version-binding header every compiled object carries.
VERSION_HEADER_FIELDS: tuple[str, ...] = (
    "schema_id",
    "schema_version",
    "contract_id",
    "contract_version",
    "methodology_version",
    "terminus_database",
    "terminus_branch",
    "terminus_commit",
    "content_hash",
    "created_by_alias",
    "created_at",
)

#: Contract Section 18 — a test record that omits any of these proves nothing.
TEST_RECORD_FIELDS: tuple[str, ...] = (
    "command",
    "environment",
    "timestamp",
    "exit_status",
    "raw_result_artifact",
    "commit_binding",
)

PERMITTED_EVIDENCE_TIERS = frozenset(t.value for t in EvidenceTier)

#: Only a deterministic verdict path may claim the deterministic tier (GP-003).
DETERMINISTIC_VERDICT_PATHS = frozenset(
    {"deterministic_oracle", "static_checker", "execution_or_state", "property_test"}
)


class EvidenceArtifactRef(BaseModel):
    """A named evidence artifact. Prose is not one of these (GP-005)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    exists: bool = False
    readable: bool = False
    #: What the result claims the artifact hashes to.
    declared_content_hash: str | None = None
    #: What the artifact actually hashes to, recomputed by the caller.
    recomputed_content_hash: str | None = None
    #: False for a prose summary; True for JSON/YAML/structured output.
    structured: bool = False


class ExecutedTestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    environment: str | None = None
    timestamp: str | None = None
    exit_status: int | None = None
    raw_result_artifact: str | None = None
    commit_binding: str | None = None


class ClaimedResult(BaseModel):
    """One claim of "done" and everything it says binds it to reality."""

    model_config = ConfigDict(extra="forbid")

    result_id: str
    header: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    evidence_artifacts: list[EvidenceArtifactRef] = Field(default_factory=list)
    evidence_tier: str | None = None
    verdict_path: str = "deterministic_oracle"
    test_record: ExecutedTestRecord | None = None
    #: ``None`` means the resolver could not answer (service outage).
    terminus_commit_resolvable: bool | None = True
    repository_commit_resolvable: bool | None = True

    @classmethod
    def from_compiled_object(
        cls,
        obj: CompiledObject,
        *,
        result_id: str,
        evidence_artifacts: list[EvidenceArtifactRef] | None = None,
        evidence_tier: str = EvidenceTier.DETERMINISTIC_ORACLE.value,
        verdict_path: str = "deterministic_oracle",
        test_record: ExecutedTestRecord | None = None,
    ) -> ClaimedResult:
        return cls(
            result_id=result_id,
            header=obj.envelope.model_dump(mode="json"),
            body=obj.body,
            evidence_artifacts=evidence_artifacts or [],
            evidence_tier=evidence_tier,
            verdict_path=verdict_path,
            test_record=test_record,
        )


class ProvenanceSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ClaimedResult] = Field(default_factory=list)
    current_contract_version: str
    terminus_service_available: bool = True
    evidence_storage_available: bool = True


def recompute_header_hash(header: dict[str, Any], body: dict[str, Any]) -> str:
    """Mirror :meth:`governance.envelope.Envelope.sealed` exactly.

    The hash must exclude itself; a self-referential hash can never verify.
    """
    base = {k: v for k, v in header.items() if k != "content_hash"}
    return content_hash({"envelope": base, "body": body})


class ProvenanceBindingOracle(DeterministicOracle):
    """Level-2 static/policy checker."""

    @property
    def oracle_id(self) -> str:
        return "ORACLE-003"

    def decide(self, subject: Any) -> Decision:
        s: ProvenanceSubject = subject

        if not s.terminus_service_available:
            return unverifiable(
                "terminus_commit_unreachable_due_to_service_outage", unresolvable_reference_count=0
            )
        if not s.evidence_storage_available:
            return unverifiable(
                "evidence_artifact_storage_unavailable", unresolvable_reference_count=0
            )

        reasons: list[str] = []
        unresolvable = 0
        stale_version = False

        for result in s.results:
            rid = result.result_id

            # --- the version header ------------------------------------
            missing = [f for f in VERSION_HEADER_FIELDS if f not in result.header]
            if missing:
                reasons.append(f"{rid}: version header missing {missing}")
            null_material = [
                f
                for f in ("schema_id", "contract_id", "contract_version", "created_by_alias")
                if not result.header.get(f)
            ]
            if null_material:
                reasons.append(f"{rid}: material header fields are empty {null_material}")

            # --- KB-002 / GP-002: the hash is recomputed, never trusted --
            declared = result.header.get("content_hash")
            if not declared:
                reasons.append(f"{rid}: no content_hash on the result header")
            else:
                recomputed = recompute_header_hash(result.header, result.body)
                if recomputed != declared:
                    reasons.append(
                        f"{rid}: content_hash {declared} does not match recomputed {recomputed}"
                    )

            # --- KB-003: stale contract version -------------------------
            declared_version = str(result.header.get("contract_version", ""))
            if declared_version and declared_version != s.current_contract_version:
                reasons.append(
                    f"{rid}: bound to contract version {declared_version}, current is "
                    f"{s.current_contract_version}"
                )
                stale_version = True

            # --- commit resolution (GP-004, KB-001) ---------------------
            for label, resolved in (
                ("terminus_commit", result.terminus_commit_resolvable),
                ("repository_commit", result.repository_commit_resolvable),
            ):
                if resolved is None or resolved is False:
                    unresolvable += 1
                    reasons.append(f"{rid}: {label} does not resolve to an existing commit")

            # --- KB-001 / GP-001 / GP-005: named, readable, hashed ------
            if not result.evidence_artifacts:
                reasons.append(f"{rid}: claims success with no named evidence artifact")
            for artifact in result.evidence_artifacts:
                if not artifact.exists or not artifact.readable:
                    unresolvable += 1
                    reasons.append(
                        f"{rid}: evidence artifact {artifact.name!r} at {artifact.path} is not "
                        "present and readable"
                    )
                    continue
                if not artifact.structured:
                    reasons.append(
                        f"{rid}: evidence artifact {artifact.name!r} is unstructured prose, not a "
                        "hashed artifact"
                    )
                if not artifact.declared_content_hash:
                    reasons.append(f"{rid}: evidence artifact {artifact.name!r} carries no hash")
                elif artifact.declared_content_hash != artifact.recomputed_content_hash:
                    reasons.append(
                        f"{rid}: evidence artifact {artifact.name!r} hash "
                        f"{artifact.declared_content_hash} != recomputed "
                        f"{artifact.recomputed_content_hash}"
                    )

            # --- KB-005: the evidence tier ------------------------------
            if result.evidence_tier not in PERMITTED_EVIDENCE_TIERS:
                reasons.append(
                    f"{rid}: evidence tier {result.evidence_tier!r} is absent or outside the "
                    "permitted five"
                )
            elif (
                result.evidence_tier == EvidenceTier.DETERMINISTIC_ORACLE.value
                and result.verdict_path not in DETERMINISTIC_VERDICT_PATHS
            ):
                # GP-003: claiming the deterministic tier for a judged result.
                reasons.append(
                    f"{rid}: claims DETERMINISTIC_ORACLE tier but its verdict path is "
                    f"{result.verdict_path!r}"
                )

            # --- KB-004: the test record --------------------------------
            if result.test_record is not None:
                empty = [
                    f
                    for f in TEST_RECORD_FIELDS
                    if getattr(result.test_record, f) is None
                ]
                if empty:
                    reasons.append(f"{rid}: test record missing {empty}")

        if reasons:
            state = (
                DriftFinding.STALE_CONTRACT_VERSION
                if stale_version and len(reasons) == 1
                else TaskState.FAILED_PROVENANCE
            )
            return fail(reasons, state, unresolvable_reference_count=unresolvable)
        return passed(
            [f"{len(s.results)} claimed results bind to contract, commit, and named evidence"],
            unresolvable_reference_count=0,
        )
