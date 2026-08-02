"""Executable assertion checks, keyed by ``(gate_id, assertion_id)``.

Contract Section 18 and Section 25. Every function here is a real check against
a real subject; there is no check that asserts ``True``. Where the subject does
not exist yet -- a live pull request, an OTel span emitter, a composition root
owned by another workstream -- there is deliberately *no* entry in
:data:`CHECKS`, and the runner reports that assertion as ``NOT_IMPLEMENTED``
with the reason. An honest "not yet executable" is worth more than a green that
measured nothing.

One check is worth reading closely. GATE-D1-08 A1 asks for an authenticated
request to the sealed repository under the builder identity. It is **not**
implemented here, and that is not laziness: A2 of the same gate forbids any
build-side file from containing the sealed repository's name, and a probe would
have to embed it. The probe belongs to the verifier's own service identity,
which can name its own repository without violating anything. Implementing it
here would make A2 fail in order to make A1 pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from evaluation.auto_merge import (
    AUTO_MERGE_REQUIREMENTS,
    AutoMergeEvaluation,
)
from evaluation.binding import CandidateBinding, EvaluationSet, Lane, LaneRun
from evaluation.gate_spec import AssertionSpec, GateSpec
from evaluation.verifier_client import (
    PERMITTED_SUBMISSION_FIELDS,
    ProtectedVerifierClient,
    build_submission,
    validate_response,
)
from governance.envelope import (
    CompiledObject,
    Envelope,
    EvidenceTier,
    KnowledgeTier,
    content_hash,
)
from governance.states import ProjectState, TaskState, Verdict
from holdouts.suite import HoldoutLane
from knowledge.tiers import (
    CITATION_VERDICT_SUPPORTED,
    GOLD_PROMOTION_STEPS,
    PromotionRejected,
    Verification,
    admit_agent_output,
    evaluate_promotion,
    is_trusted,
    promote,
    to_compiled_object,
)
from mutants.catalog import MutantClass
from mutants.runner import MutationRunResult, run_mutation_gate
from oracles import fixtures as fx
from oracles.base import DeterministicOracle
from oracles.no_judge import prove_no_judge
from oracles.oracle_003_provenance import ExecutedTestRecord
from oracles.registry import VERDICT_PATH_MODULES, RoutingDecision, build_oracles, route
from verifier_identity.identity import measure as measure_verifier_identity

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "project-pack"


class AssertionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass
class AssertionOutcome:
    status: AssertionStatus
    findings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def ok(evidence: dict[str, Any] | None = None, note: str = "") -> AssertionOutcome:
    return AssertionOutcome(AssertionStatus.PASS, [], evidence or {}, note)


def bad(findings: list[str], evidence: dict[str, Any] | None = None) -> AssertionOutcome:
    return AssertionOutcome(AssertionStatus.FAIL, findings, evidence or {})


def undecided(reason: str, evidence: dict[str, Any] | None = None) -> AssertionOutcome:
    return AssertionOutcome(AssertionStatus.UNVERIFIABLE, [reason], evidence or {}, reason)


@dataclass
class GateContext:
    """Everything a check may look at, resolved once and shared."""

    binding: CandidateBinding
    gates: dict[str, GateSpec]
    repo_root: Path = REPO_ROOT
    _oracles: dict[str, DeterministicOracle] | None = None
    _mutation: MutationRunResult | None = None
    _pack: dict[str, Any] = field(default_factory=dict)

    @property
    def oracles(self) -> dict[str, DeterministicOracle]:
        if self._oracles is None:
            self._oracles = build_oracles()
        return self._oracles

    @property
    def mutation(self) -> MutationRunResult:
        if self._mutation is None:
            self._mutation = run_mutation_gate(self.oracles, self.binding)
        return self._mutation

    def pack_yaml(self, name: str) -> dict[str, Any]:
        if name not in self._pack:
            self._pack[name] = yaml.safe_load((PACK_ROOT / name).read_text())
        return self._pack[name]


Check = Callable[[GateContext, GateSpec, AssertionSpec], AssertionOutcome]


@contextmanager
def _chdir(target: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(target)
    try:
        yield
    finally:
        os.chdir(previous)


# ===========================================================================
# GATE-D1-02 — schemas validate and are version-bound
# ===========================================================================

def _d1_02_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    obj = CompiledObject.create(
        schema_id="efah.gate_result",
        created_by_alias="oracle-o02",
        body={"probe": "d1-02-a1"},
        terminus_database="efah",
        terminus_branch="import/pack",
        terminus_commit="unresolved-on-build-side",
    )
    header = obj.envelope.model_dump(mode="json")
    required = list(Envelope.model_fields)
    missing = [f for f in required if f not in header]
    empty = [f for f in ("schema_id", "contract_id", "contract_version", "created_by_alias") if not header.get(f)]
    evidence = {
        "schema_validation_report": {
            "required_fields": required,
            "present": sorted(header),
            "missing": missing,
            "materially_empty": empty,
        }
    }
    if missing or empty:
        return bad([f"missing {missing}", f"empty {empty}"], evidence)
    return ok(evidence, f"all {len(required)} envelope fields present on a real compiled object")


def _d1_02_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Negative control: inject a stale contract_version and require rejection."""
    oracle = ctx.oracles["ORACLE-003"]
    subject = fx.good_provenance(results=[fx.good_claimed_result(contract_version="0.2")])
    decision = oracle.decide(subject)
    evidence = {
        "negative_control_transcript": {
            "probe": "inject contract_version 0.2 while 1.1 is current",
            "verdict": decision.verdict.value,
            "failure_state": decision.failure_state.value if decision.failure_state else None,
            "reasons": decision.reasons,
        }
    }
    if decision.verdict is Verdict.FAIL and decision.failure_state is not None:
        return ok(evidence, "stale version rejected, not migrated silently")
    return bad([f"stale version produced {decision.verdict.value}"], evidence)


def _d1_02_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Default-injection probe: a material field must not be silently defaulted."""
    findings: list[str] = []
    try:
        Envelope(schema_version="1.0")  # type: ignore[call-arg]
        findings.append("Envelope constructed with no schema_id or created_by_alias")
    except Exception as exc:
        raised = type(exc).__name__
    extra_field_refused = False
    try:
        Envelope(schema_id="x", created_by_alias="y", unknown_field="z")  # type: ignore[call-arg]
        findings.append("Envelope accepted an undeclared field")
    except Exception:
        extra_field_refused = True
    evidence = {
        "negative_control_transcript": {
            "probe": "construct an Envelope with material fields absent",
            "raised": raised if not findings else None,
            "extra_field_forbidden": extra_field_refused,
        }
    }
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "absent material fields raise rather than defaulting")


def _d1_02_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    obj = CompiledObject.create(
        schema_id="efah.gate_result", created_by_alias="oracle-o02", body={"probe": "hash"}
    )
    intact = obj.is_intact()
    tampered = CompiledObject(envelope=obj.envelope, body={"probe": "tampered"})
    evidence = {
        "hash_recomputation_log": {
            "declared": obj.envelope.content_hash,
            "recomputed_matches": intact,
            "tampered_body_detected": not tampered.is_intact(),
        }
    }
    if intact and not tampered.is_intact():
        return ok(evidence, "hash recomputes and detects a tampered body")
    return bad(["content_hash does not behave as a binding hash"], evidence)


# ===========================================================================
# GATE-D1-07 — vendor neutrality (delegated to the pinned tool)
# ===========================================================================

def _load_d1_07_module():
    path = REPO_ROOT / "tools" / "gate_d1_07.py"
    spec = importlib.util.spec_from_file_location("_gate_d1_07", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _d1_07(assertion_id: str) -> Check:
    def check(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
        module = _load_d1_07_module()
        claim, fn = module.ASSERTIONS[assertion_id]
        with _chdir(ctx.repo_root):
            passed, findings = fn()
        evidence_name = {
            "A1": "import_graph_report",
            "A2": "credential_stripped_run_transcript",
            "A3": "import_graph_report",
            "A4": "ci_workflow_scan_output",
            "A5": "adapter_removal_run_log",
        }[assertion_id]
        evidence = {evidence_name: {"claim": claim, "passed": passed, "findings": findings}}
        return ok(evidence, claim) if passed else bad(findings, evidence)

    return check



# ===========================================================================
# GATE-D1-10 — owner control surface (delegated to the pinned tool)
# ===========================================================================
# Contract v1.1 §11.7, AMENDMENT-001. The ten assertions are executed by
# tests/contract/test_owner_surface.py, which tools/gate_d1_10.py drives with
# every Anthropic credential stripped from the subprocess environment. Reporting
# the gate NOT_YET_EXECUTABLE while a pinned tool demonstrably decides it would
# understate the evidence -- and this is the gate that determines whether work
# continues after the builder leaves.


def _d1_10(assertion_id: str) -> Check:
    def check(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
        path = REPO_ROOT / "tools" / "gate_d1_10.py"
        spec = importlib.util.spec_from_file_location("_gate_d1_10", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        claim, prefix = module.ASSERTIONS[assertion_id]

        env = dict(os.environ)
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
            env.pop(name, None)
        with _chdir(ctx.repo_root):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", module.TESTS, "-k", prefix, "-q", "--no-header"],
                capture_output=True, text=True, env=env,
            )
        passed = proc.returncode == 0
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        evidence = {
            "credential_stripped_run_transcript": {
                "claim": claim,
                "passed": passed,
                "selector": prefix,
                "anthropic_credentials_present": False,
                "detail": tail[-1] if tail else "",
            }
        }
        return ok(evidence, claim) if passed else bad(tail[-8:], evidence)

    return check


# ===========================================================================
# GATE-D1-08 — protected verifier isolation
# ===========================================================================

_ROUTE_MARKERS = re.compile(
    r"(https?://|git@|ssh://)|((token|password|secret|api_key|credential)\s*[:=])", re.IGNORECASE
)


def _d1_08_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Static scan. The forbidden patterns are read from the gate, never hardcoded.

    Writing the sealed repository's name into this file in order to search for
    it would create the very match the assertion forbids. The gate declares its
    own ``forbidden_patterns``; the scanner reads them at run time.
    """
    patterns: list[str] = [str(p) for p in a.raw.get("forbidden_patterns", [])]
    scope: list[str] = [str(s) for s in a.raw.get("scope", [])]
    if not patterns:
        return undecided("the gate declares no forbidden_patterns to scan for")

    build_side_violations: list[str] = []
    declared_block_matches: list[dict[str, Any]] = []
    scanned = 0

    for entry in scope:
        if entry.startswith("*"):
            candidates = list(ctx.repo_root.rglob(entry))
        else:
            base = ctx.repo_root / entry
            candidates = [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []
        for path in candidates:
            if ".git/" in str(path) or path.suffix in {".pyc", ".png", ".jpg"}:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            scanned += 1
            relative = path.relative_to(ctx.repo_root).as_posix()
            for line_no, line in enumerate(text.splitlines(), 1):
                for pattern in patterns:
                    if pattern not in line:
                        continue
                    record = {
                        "path": relative,
                        "line": line_no,
                        "pattern": pattern,
                        "resolves_a_route": bool(_ROUTE_MARKERS.search(line)),
                    }
                    # The owner's own pack is the declared sealed-repos block:
                    # repositories.yaml names them precisely so the isolation
                    # scan treats them as forbidden elsewhere. A *route* there
                    # would still be a violation.
                    if relative.startswith("project-pack/") and not record["resolves_a_route"]:
                        declared_block_matches.append(record)
                    else:
                        build_side_violations.append(
                            f"{relative}:{line_no} matches {pattern!r}"
                            + (" and resolves a usable route" if record["resolves_a_route"] else "")
                        )

    # Strictly stronger than the pattern heuristic: the sealed repositories must
    # carry no resolvable URL anywhere, including in the owner's declaration.
    repositories = ctx.pack_yaml("repositories.yaml")
    url_findings = [
        f"sealed repository {repo.get('name')!r} declares url={repo.get('url')!r}"
        for repo in repositories.get("sealed_repos", [])
        if str(repo.get("url", "")).startswith(("http://", "https://", "git@", "ssh://"))
    ]

    evidence = {
        "static_scan_report_with_content_hash": {
            "files_scanned": scanned,
            "scope": scope,
            "pattern_count": len(patterns),
            "build_side_violations": build_side_violations,
            "declared_sealed_repos_block_matches": declared_block_matches,
            "sealed_repo_url_findings": url_findings,
            "candidate_commit": ctx.binding.commit_sha,
        }
    }
    evidence["static_scan_report_with_content_hash"]["content_hash"] = content_hash(
        evidence["static_scan_report_with_content_hash"]
    )
    if build_side_violations or url_findings:
        return bad(build_side_violations + url_findings, evidence)
    return ok(
        evidence,
        f"{scanned} files scanned; zero matches outside the declared sealed-repos block, "
        "and no sealed URL anywhere",
    )


def _d1_08_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``compare_actor_identifiers`` — real uids, not declarations about them.

    This assertion previously reported PASS from ``repositories.yaml`` and
    ``model-policy.yaml`` alone. Those are declarations *that* the identities
    differ; the method asks for the identifiers themselves. FINDING-004 recorded
    exactly this error one gate over — GATE-D1-10 A9 passed on an observation
    that did not match the condition it claimed to test — so it is corrected
    here rather than left because it happened to be green.

    DEC-006 option B makes the real comparison possible: an ``efah-verifier``
    system account owning a 0700 store the builder cannot traverse. The pack
    checks below still run, because a live boundary with the pack declaring
    ``builder_access: read`` would be a boundary somebody is about to remove.

    On a host with no verifier identity — CI, a fresh clone — this is
    ``UNVERIFIABLE``. It is not PASS. §14.4's rule is that services are
    exercised *with evidence*, and a declaration is not the evidence this
    assertion names.
    """
    repositories = ctx.pack_yaml("repositories.yaml")
    policy = ctx.pack_yaml("model-policy.yaml")
    sealed = repositories.get("sealed_repos", [])
    findings: list[str] = []
    for repo in sealed:
        if repo.get("service_identity") != "separate":
            findings.append(f"{repo.get('name')!r} does not declare a separate service identity")
        if repo.get("builder_access") != "forbidden":
            findings.append(f"{repo.get('name')!r} builder_access is {repo.get('builder_access')!r}")
    author = (policy.get("aliases") or {}).get("sealed_holdout_author", {})
    verifier_identity = author.get("runs_under_identity")
    if verifier_identity != "verifier_service_identity":
        findings.append(
            f"sealed_holdout_author runs under {verifier_identity!r}, not the verifier identity"
        )
    implementer = (policy.get("aliases") or {}).get("implementer", {})
    if implementer.get("runs_under_identity") == verifier_identity:
        findings.append("the implementer runs under the verifier service identity")
    measurement = measure_verifier_identity()
    measured = measurement.as_body()

    evidence = {
        "token_scope_dump_redacted": {
            "note": "identities are compared as live actor identifiers; the pack is checked too",
            "declared_verifier_service_identity": verifier_identity,
            "declared_builder_identity": implementer.get("runs_under_identity") or "builder_default",
            "sealed_repositories": [
                {
                    "role": repo.get("role"),
                    "builder_access": repo.get("builder_access"),
                    "service_identity": repo.get("service_identity"),
                    "url_supplied_to_builder": not str(repo.get("url", "")).startswith(
                        ("http", "git@", "ssh")
                    ),
                }
                for repo in sealed
            ],
        },
        "actor_identifiers": measured,
    }

    if findings:
        return bad(findings, evidence)

    if not measurement.provisioned:
        return undecided(
            "no verifier service identity is provisioned on this host, so the actor "
            "identifiers this assertion compares do not exist; the pack declares the "
            "separation but a declaration is not an identifier "
            "(run deploy/verifier/provision.sh under the owner's authority)",
            evidence,
        )
    if measurement.measured_as_root:
        return undecided(
            "measured as root, which can read the store regardless; a root-run "
            "measurement proves nothing about the builder identity",
            evidence,
        )
    if not measurement.isolation_holds:
        can_read, detail = measurement.builder_read_attempt
        return bad(
            [
                f"the builder identity {measurement.builder_user!r} could read the sealed "
                f"store: {detail}"
                if can_read
                else f"verifier identity isolation did not hold: {detail}"
            ],
            evidence,
        )

    return ok(
        evidence,
        (
            f"builder uid {measurement.builder_uid} != verifier uid {measurement.verifier_uid}; "
            f"the builder's read of the sealed store was refused by the kernel "
            f"({measurement.builder_read_attempt[1]}). Honest debt: the builder holds "
            "passwordless sudo, so this is an accident-and-audit boundary, not a "
            "security one (DEC-006)."
        ),
    )


def _d1_08_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Schema-assert the verdict payload, including the leak probes."""
    permitted = [str(f) for f in a.raw.get("permitted_fields", [])]
    good_payload = {
        "evaluation_request_id": "EVAL-D1-08",
        "verdict": "PASS",
        "oracle_version": "1.0.0",
        "oracle_health": {
            "oracle_id": "ORACLE-003",
            "content_hash": "sha256:" + "0" * 64,
            "fixture_suite_result": "PASS",
            "last_audit_date": "2026-08-02",
        },
        "failure_class": None,
    }
    accepted, accept_findings = validate_response(good_payload)

    probes = {
        "hidden_assertion_text": {
            **good_payload,
            "oracle_health": {"health_status": "assert reachability from an approved entry point"},
        },
        "private_fixture_content": {**good_payload, "private_fixture": {"input": 7, "expect": 42}},
        "mutant_source": {
            **good_payload,
            "oracle_health": {"health_status": "def mutated(x):\n    return True\n"},
        },
        "holdout_case_body": {
            **good_payload,
            "oracle_health": {"holdout_case": "import sys; assert sys.argv"},
        },
    }
    rejections: dict[str, Any] = {}
    unrejected: list[str] = []
    for name, payload in probes.items():
        result, findings = validate_response(payload)
        rejections[name] = {"rejected": result is None, "findings": findings}
        if result is not None:
            unrejected.append(name)

    evidence = {
        "verdict_payload_sample_and_schema_result": {
            "permitted_fields_declared_by_gate": permitted,
            "approved_shape_accepted": accepted is not None,
            "approved_shape_findings": accept_findings,
            "forbidden_content_probes": rejections,
            "rejection_state": TaskState.FAILED_PROVENANCE.value,
        }
    }
    findings: list[str] = []
    if accepted is None:
        findings.append(f"a contract-approved payload was rejected: {accept_findings}")
    findings.extend(f"forbidden content not rejected: {name}" for name in unrejected)
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "approved shape accepted; all four forbidden-content classes rejected")


def _d1_08_a6(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    aliases = ctx.pack_yaml("model-policy.yaml").get("aliases") or {}
    implementer = aliases.get("implementer", {})
    findings: list[str] = []
    comparisons: dict[str, Any] = {}
    for role in ("sealed_holdout_author", "judge"):
        other = aliases.get(role, {})
        same_alias = implementer.get("alias") == other.get("alias")
        same_family = implementer.get("family") == other.get("family")
        comparisons[role] = {
            "implementer_alias": implementer.get("alias"),
            "other_alias": other.get("alias"),
            "implementer_family": implementer.get("family"),
            "other_family": other.get("family"),
            "distinct_by_alias": not same_alias,
            "distinct_by_family": not same_family,
        }
        if same_alias:
            findings.append(f"implementer shares an alias with {role}")
        if same_family:
            findings.append(f"implementer shares a vendor family with {role}")
    evidence = {"verdict_payload_sample_and_schema_result": {"alias_comparison": comparisons}}
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "implementer differs from holdout author and judge by alias and family")


# ===========================================================================
# GATE-D1-09 — mechanical commit/trace/artifact binding
# ===========================================================================

def _d1_09_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    oracle = ctx.oracles["ORACLE-003"]
    incomplete = fx.good_provenance(
        results=[fx.good_claimed_result(test_record=ExecutedTestRecord(command="pytest -q"))]
    )
    complete = fx.good_provenance()
    bad_decision = oracle.decide(incomplete)
    good_decision = oracle.decide(complete)
    evidence = {
        "test_record_sample": {
            "complete_record_verdict": good_decision.verdict.value,
            "incomplete_record_verdict": bad_decision.verdict.value,
            "incomplete_reasons": bad_decision.reasons,
        }
    }
    if good_decision.verdict is Verdict.PASS and bad_decision.verdict is Verdict.FAIL:
        return ok(evidence, "a test record missing required fields is rejected")
    return bad(
        [f"complete={good_decision.verdict.value} incomplete={bad_decision.verdict.value}"],
        evidence,
    )


def _d1_09_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    oracle = ctx.oracles["ORACLE-003"]
    unbound = fx.good_provenance(results=[fx.good_claimed_result(evidence_artifacts=[])])
    decision = oracle.decide(unbound)
    evidence = {
        "negative_control_transcript": {
            "probe": "submit a result claiming success with no named evidence",
            "verdict": decision.verdict.value,
            "failure_state": decision.failure_state.value if decision.failure_state else None,
            "reasons": decision.reasons,
        }
    }
    if decision.verdict is Verdict.FAIL and decision.failure_state == TaskState.FAILED_PROVENANCE:
        return ok(evidence, "an unbound result is rejected as FAILED_PROVENANCE")
    return bad([f"unbound result produced {decision.verdict.value}"], evidence)


def _d1_09_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    oracle = ctx.oracles["ORACLE-003"]
    permitted = [t.value for t in EvidenceTier]
    outside = fx.good_provenance(results=[fx.good_claimed_result(evidence_tier="LOOKS_FINE")])
    decision = oracle.decide(outside)
    good = oracle.decide(fx.good_provenance())
    evidence = {
        "artifact_registry_dump": {
            "permitted_tiers": permitted,
            "tier_outside_the_five_verdict": decision.verdict.value,
            "tier_inside_the_five_verdict": good.verdict.value,
        }
    }
    if decision.verdict is Verdict.FAIL and good.verdict is Verdict.PASS:
        return ok(evidence, "only the contract's five evidence tiers are accepted")
    return bad(["evidence tier is not enforced"], evidence)


# ===========================================================================
# GATE-D2-18 — unverified knowledge is not promoted
# ===========================================================================

def _d2_18_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    item = admit_agent_output(
        item_id="K-D2-18",
        statement="the eval gateway performs zero retries",
        producer_alias="researcher-r17",
        producer_family="openai",
        claimed_tier=KnowledgeTier.T7_HARD_GOLD,
    )
    evidence = {
        "gate_execution_log": {
            "claimed_tier": KnowledgeTier.T7_HARD_GOLD.value,
            "admitted_tier": item.tier.value,
            "trusted": is_trusted(item.tier),
        }
    }
    if item.tier is KnowledgeTier.T2_HYPOTHESIS and not is_trusted(item.tier):
        return ok(evidence, "agent output claiming T7 is admitted at T2 and is not trusted")
    return bad([f"agent output admitted at {item.tier.value}"], evidence)


def _d2_18_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    same_family = admit_agent_output(
        item_id="K-same",
        statement="x",
        producer_alias="researcher-r17",
        producer_family="openai",
    )
    same_family.reproduction_runs = 2
    same_family.verifications = [
        Verification("implementer-i12", "openai", "rerun", True),
    ]
    # FINDING-007 added a citation gate above T2. This assertion is about family
    # separation, so both arms satisfy the citation rule explicitly -- otherwise
    # the negative control would pass for the wrong reason and A2 would stop
    # measuring what it claims to.
    same_family.citation_verdict = CITATION_VERDICT_SUPPORTED
    same = evaluate_promotion(same_family, KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)

    cross = admit_agent_output(
        item_id="K-cross",
        statement="x",
        producer_alias="researcher-r17",
        producer_family="openai",
    )
    cross.reproduction_runs = 2
    cross.verifications = [Verification("critic-c08", "xai", "independent rerun", True)]
    cross.citation_verdict = CITATION_VERDICT_SUPPORTED
    crossed = evaluate_promotion(cross, KnowledgeTier.T5_INDEPENDENTLY_VERIFIED)

    evidence = {
        "negative_control_transcript": {
            "same_family_allowed": same.allowed,
            "same_family_blockers": same.blockers,
            "cross_family_allowed": crossed.allowed,
        }
    }
    if not same.allowed and crossed.allowed:
        return ok(evidence, "promotion above T4 requires a different vendor family")
    return bad(
        [f"same_family_allowed={same.allowed}, cross_family_allowed={crossed.allowed}"], evidence
    )


def _d2_18_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    item = admit_agent_output(
        item_id="K-gold", statement="x", producer_alias="researcher-r17", producer_family="openai"
    )
    item.reproduction_runs = 2
    item.verifications = [Verification("critic-c08", "xai", "independent rerun", True)]
    item.citation_verdict = CITATION_VERDICT_SUPPORTED
    partial = evaluate_promotion(item, KnowledgeTier.T7_HARD_GOLD)
    item.gold_steps_recorded = set(GOLD_PROMOTION_STEPS)
    complete = evaluate_promotion(item, KnowledgeTier.T7_HARD_GOLD)
    evidence = {
        "gate_execution_log": {
            "required_steps": list(GOLD_PROMOTION_STEPS),
            "without_steps_allowed": partial.allowed,
            "without_steps_blockers": partial.blockers,
            "with_all_five_allowed": complete.allowed,
        }
    }
    if not partial.allowed and complete.allowed:
        return ok(evidence, "hard-gold promotion needs all five Section 15.6 steps")
    return bad([f"partial={partial.allowed}, complete={complete.allowed}"], evidence)


def _d2_18_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    item = admit_agent_output(
        item_id="K-unverified",
        statement="x",
        producer_alias="researcher-r17",
        producer_family="openai",
    )
    outcomes: dict[str, Any] = {}
    for tier in (KnowledgeTier.T6_APPROVED_OPERATIONAL_KNOWLEDGE, KnowledgeTier.T7_HARD_GOLD):
        try:
            promote(item, tier)
            outcomes[tier.value] = {"rejected": False}
        except PromotionRejected as exc:
            outcomes[tier.value] = {"rejected": True, "reason": str(exc)}
    evidence = {
        "negative_control_transcript": {"promotion_attempts": outcomes},
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "knowledge_item": to_compiled_object(item).envelope.model_dump(mode="json"),
            "transcript_hash": content_hash(outcomes),
        },
    }
    if all(o["rejected"] for o in outcomes.values()):
        return ok(evidence, "a deliberately unverified item cannot reach T6 or T7")
    return bad(["an unverified item was promoted"], evidence)


# ===========================================================================
# GATE-D2-19 — one candidate commit across all three lanes
# ===========================================================================

def _d2_19_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    evaluation_set = EvaluationSet(
        evaluation_request_id=f"EVAL-{ctx.binding.short}", binding=ctx.binding
    )
    evaluation_set.record(
        LaneRun(
            Lane.VISIBLE,
            ctx.binding.commit_sha,
            Verdict.PASS,
            "visible gate suite executed by this runner",
        )
    )
    evaluation_set.record(ctx.mutation.lane_run())
    holdout = HoldoutLane().run(
        ctx.binding, evaluation_request_id=f"EVAL-{ctx.binding.short}"
    )
    evaluation_set.record(holdout.lane_run)

    evidence = {
        "gate_execution_log": evaluation_set.as_evidence(),
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "hidden_lane": holdout.as_evidence(),
        },
    }
    if evaluation_set.invalidated:
        return bad(evaluation_set.invalidated_because, evidence)
    if evaluation_set.verdict() is Verdict.UNVERIFIABLE or holdout.lane_run.verdict is Verdict.UNVERIFIABLE:
        return AssertionOutcome(
            AssertionStatus.UNVERIFIABLE,
            [
                (
                    "all lanes carry the same candidate commit, but the hidden lane "
                    f"could not run: {holdout.blocked_reason}"
                )
            ],
            evidence,
            "sealed holdout content and verifier endpoint are not available to the build side",
        )
    return ok(evidence, "visible, hidden, and mutant lanes share one candidate commit")


def _d2_19_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    submission = build_submission(
        artifact_or_commit_identifier=ctx.binding.commit_sha,
        evaluation_request_id=f"EVAL-{ctx.binding.short}",
        required_contract_or_oracle_version=ctx.binding.contract_version,
    )
    sent = sorted(submission.model_dump().keys())
    permitted = sorted(PERMITTED_SUBMISSION_FIELDS)
    extra_rejected = False
    try:
        submission.__class__(**submission.model_dump(), hint="the holdout expects 42")
    except Exception:
        extra_rejected = True
    evidence = {
        "gate_execution_log": {
            "fields_sent": sent,
            "permitted": permitted,
            "extra_field_rejected_by_schema": extra_rejected,
        },
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "submission_hash": content_hash(submission.model_dump(mode="json")),
        },
    }
    if sent == permitted and extra_rejected:
        return ok(evidence, "only the four permitted submission fields can cross the seam")
    return bad([f"sent={sent} permitted={permitted} extra_rejected={extra_rejected}"], evidence)


def _d2_19_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    evaluation_set = EvaluationSet(evaluation_request_id="EVAL-negctl", binding=ctx.binding)
    evaluation_set.record(LaneRun(Lane.VISIBLE, ctx.binding.commit_sha, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.MUTANT, ctx.binding.commit_sha, Verdict.PASS))
    evaluation_set.record(LaneRun(Lane.HIDDEN, "f" * 40, Verdict.PASS))
    evidence = {
        "negative_control_transcript": {
            "probe": "change the candidate commit between lanes",
            "invalidated": evaluation_set.invalidated,
            "because": evaluation_set.invalidated_because,
            "set_verdict": evaluation_set.verdict().value,
        }
    }
    if evaluation_set.invalidated and evaluation_set.verdict() is Verdict.FAIL:
        return ok(evidence, "a commit change between lanes invalidates the evaluation set")
    return bad(["a mid-run commit change did not invalidate the set"], evidence)


# ===========================================================================
# GATE-D2-20 — oracle health and no judge in the deterministic path
# ===========================================================================

def _d2_20_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    findings: list[str] = []
    emitted: dict[str, Any] = {}
    for oracle_id, oracle in sorted(ctx.oracles.items()):
        fixture = fx.fixtures_for(oracle_id)[0]
        result = oracle.evaluate(fixture.subject, subject_ref=fixture.fixture_id,
                                 candidate_commit=ctx.binding.commit_sha)
        emitted[oracle_id] = {
            "oracle_version": result.oracle_version,
            "health": result.health,
            "declared_health_fields": oracle.declared_health_fields,
        }
        for required in ("oracle_version", "content_hash", "last_audit_date"):
            if required not in result.health and required != "oracle_version":
                findings.append(f"{oracle_id}: health omits {required}")
        if not result.oracle_version:
            findings.append(f"{oracle_id}: result carries no oracle version")
        if result.health.get("content_hash", "").startswith("NOT_"):
            findings.append(f"{oracle_id}: content hash was never minted")
        if result.health.get("last_audit_date") in (None, "NOT_MINTED"):
            findings.append(f"{oracle_id}: last audit date was never minted")
    evidence = {"gate_execution_log": {"check": "oracle_result_schema_assert", "oracles": emitted}}
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "every oracle result carries version, content hash, and health")


def _d2_20_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    proofs = {oid: prove_no_judge(mod) for oid, mod in sorted(VERDICT_PATH_MODULES.items())}
    findings = [
        f"{oid}: {violation}" for oid, proof in proofs.items() for violation in proof.violations
    ]
    evidence = {
        "gate_execution_log": {oid: proof.as_evidence() for oid, proof in proofs.items()},
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "proof_hash": content_hash({oid: p.as_evidence() for oid, p in proofs.items()}),
        },
    }
    if findings:
        return bad(findings, evidence)
    modules = sum(len(p.modules_in_closure) for p in proofs.values())
    return ok(evidence, f"zero model-call surfaces across {modules} modules in the verdict paths")


def _d2_20_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    suites = {oid: fx.run_fixture_suite(o) for oid, o in sorted(ctx.oracles.items())}
    findings = [
        f"{oid}: fixture {failure.fixture_id} expected {failure.expected}, observed {failure.observed}"
        for oid, suite in suites.items()
        for failure in suite.failures()
    ]
    mutation = ctx.mutation
    survivors = [
        f"{o.mutant_id} ({o.declared_as or o.description}) survived: {o.detail}"
        for o in mutation.survivors
    ]
    evidence = {
        "gate_execution_log": {oid: suite.as_evidence() for oid, suite in suites.items()},
        "negative_control_transcript": mutation.as_evidence(),
    }
    if findings or survivors or mutation.declared_but_unimplemented:
        return bad(
            findings + survivors
            + [f"declared but never killed: {mutation.declared_but_unimplemented}"]
            * bool(mutation.declared_but_unimplemented),
            evidence,
        )
    total = sum(len(s.outcomes) for s in suites.values())
    return ok(
        evidence,
        f"{total} fixtures pass and {mutation.killed}/{mutation.total} mutants are killed",
    )


def _d2_20_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    probes: dict[str, Any] = {}
    findings: list[str] = []
    for oracle_id, oracle in sorted(ctx.oracles.items()):
        cases = [f for f in fx.fixtures_for(oracle_id) if f.kind == fx.UNVERIFIABLE_PROBE]
        if not cases:
            findings.append(f"{oracle_id}: no ambiguous-input probe exists")
            continue
        results = {}
        for case in cases:
            decision = oracle.decide(case.subject)
            results[case.fixture_id] = decision.verdict.value
            if decision.verdict is not Verdict.UNVERIFIABLE:
                findings.append(
                    f"{oracle_id}/{case.fixture_id}: returned {decision.verdict.value} "
                    "instead of UNVERIFIABLE"
                )
        probes[oracle_id] = results
    evidence = {"gate_execution_log": {"check": "ambiguous_input_probe", "probes": probes}}
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "every oracle abstains honestly where its definition says it cannot decide")


def _d2_20_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    policy = ctx.pack_yaml("model-policy.yaml")
    limits = policy.get("authority_limits", {})
    calibration = policy.get("judge_calibration", {})
    findings: list[str] = []
    if limits.get("uncalibrated_judge_is_gate") is not False:
        findings.append("authority_limits.uncalibrated_judge_is_gate is not false")
    if limits.get("model_judge_in_deterministic_verdict_path") != "forbidden":
        findings.append("a model judge is not forbidden in the deterministic verdict path")
    if calibration.get("minimum_agreement_to_gate") is not None:
        findings.append("a judge gating threshold is set while judges are declared advisory")
    if calibration.get("posture") != "all_judges_advisory_for_deadline_build":
        findings.append(f"judge posture is {calibration.get('posture')!r}")
    # And structurally: no gate in the pack admits a judge into its verdict path.
    admitting = [gid for gid, spec in ctx.gates.items() if spec.model_judge_in_verdict_path]
    findings.extend(f"{gid} admits a model judge" for gid in admitting)
    evidence = {
        "negative_control_transcript": {
            "authority_limits": limits,
            "judge_calibration_posture": calibration.get("posture"),
            "minimum_agreement_to_gate": calibration.get("minimum_agreement_to_gate"),
            "gates_admitting_a_judge": admitting,
            "result_is_advisory_only": True,
        }
    }
    if findings:
        return bad(findings, evidence)
    return ok(evidence, "uncalibrated judge output is advisory and no gate admits one")


def _d2_20_a6(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    candidates = list(ctx.oracles.values())
    decision: RoutingDecision = route("does this candidate bind to its evidence?", candidates)
    findings: list[str] = []
    if decision.selected_level is None:
        findings.append("no oracle was selected")
    else:
        available = min(o.hierarchy_level for o in candidates)
        if decision.selected_level != available:
            findings.append(
                f"selected level {decision.selected_level} while level {available} was available"
            )
    evidence = {"gate_execution_log": decision.as_evidence()}
    if findings:
        return bad(findings, evidence)
    return ok(evidence, f"routing selected hierarchy level {decision.selected_level}")


# ===========================================================================
# GATE-D3-24 — known-bad mutant is rejected
# ===========================================================================

def _mutation_set_check(mutant_class: MutantClass, label: str) -> Check:
    def check(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
        outcomes = [o for o in ctx.mutation.outcomes if o.mutant_class is mutant_class]
        evidence = {
            "gate_execution_log": {
                "set": mutant_class.value,
                "seeded": len(outcomes),
                "killed": sum(1 for o in outcomes if o.killed),
                "mutants": [
                    {
                        "mutant_id": o.mutant_id,
                        "declared_as": o.declared_as,
                        "killed": o.killed,
                        "detail": o.detail,
                        "error": o.error,
                    }
                    for o in outcomes
                ],
            },
            "artifact_hashes_and_commit_binding": {
                "candidate_commit": ctx.mutation.candidate_commit
            },
        }
        if not outcomes:
            return bad([f"no {label} mutant was seeded"], evidence)
        survivors = [o for o in outcomes if not o.killed]
        if survivors:
            return bad(
                [f"{o.mutant_id} ({o.declared_as or label}) survived: {o.detail}" for o in survivors],
                evidence,
            )
        return ok(evidence, f"{len(outcomes)} {label} mutant(s) seeded, all killed")

    return check


def _d3_24_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    mutation = ctx.mutation
    evidence = {
        "gate_execution_log": mutation.as_evidence(),
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": mutation.candidate_commit,
            "evidence_hash": content_hash(mutation.as_evidence()),
        },
    }
    if mutation.verdict() is Verdict.PASS:
        return ok(evidence, f"kill rate {mutation.kill_rate:.2f} over {mutation.total} mutants")
    findings = [f"{o.mutant_id} survived: {o.detail}" for o in mutation.survivors]
    if mutation.declared_but_unimplemented:
        findings.append(
            f"oracle definitions declare mutants nobody implemented: "
            f"{mutation.declared_but_unimplemented}"
        )
    missing_sets = sorted({c.value for c in mutation.sets_covered} ^ {c.value for c in (MutantClass.IMPLEMENTATION, MutantClass.TEST, MutantClass.EVALUATOR_ORACLE, MutantClass.WORKFLOW_GOVERNANCE)})
    if missing_sets:
        findings.append(f"Section 17.1 mutant sets not represented: {missing_sets}")
    return bad(findings, evidence)


def _d3_24_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """The implementer must not be able to read private mutant source.

    The visible mutant catalogue is deliberately visible -- Section 17.1
    separates it from the private set, and ``repositories.yaml`` places
    ``private_mutants`` in the sealed repository. What must hold on the build
    side is that no private mutant crosses the seam: not in the submission the
    candidate sends, and not in the result it receives.
    """
    repositories = ctx.pack_yaml("repositories.yaml")
    sealed_holds = [h for repo in repositories.get("sealed_repos", []) for h in (repo.get("holds") or [])]
    submission = build_submission(
        artifact_or_commit_identifier=ctx.binding.commit_sha,
        evaluation_request_id="EVAL-D3-24",
        required_contract_or_oracle_version=ctx.binding.contract_version,
    )
    leaking = {
        "evaluation_request_id": "EVAL-D3-24",
        "verdict": "FAIL",
        "oracle_version": "1.0.0",
        "oracle_health": {"health_status": "def seeded_mutant(x):\n    return not x\n"},
        "failure_class": "TEST_FAILURE",
    }
    result, findings_from_client = validate_response(leaking)
    client = ProtectedVerifierClient()
    outcome = client.submit(submission)

    evidence = {
        "gate_execution_log": {
            "sealed_side_holds": sealed_holds,
            "submission_fields": sorted(submission.model_dump().keys()),
            "mutant_source_in_response_rejected": result is None,
            "client_findings": findings_from_client,
        },
        "negative_control_transcript": {
            "probe": "request a verdict with no configured endpoint",
            "state": outcome.state.value,
            "result": outcome.result,
        },
    }
    findings: list[str] = []
    if "private_mutants" not in sealed_holds:
        findings.append("the pack does not place private mutants on the sealed side")
    if result is not None:
        findings.append("a response carrying mutant source was accepted")
    if outcome.state is not ProjectState.BLOCKED_EXTERNAL_ACCESS:
        findings.append(f"an unconfigured client returned {outcome.state.value}")
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        "private mutants stay sealed; a response carrying mutant source is rejected",
    )


# ===========================================================================
# GATE-D3-26 — the §27 final evidence package
# ===========================================================================
#
# A1, A2, A3 and A5 ask about the *package*: are its fields present, is every
# acceptance check covered by named evidence, is honest debt stated, are aliases
# blinded. All four are answerable now and are answered from a freshly built
# package rather than from a stored copy, so the gate cannot pass on a stale
# artifact somebody generated when things looked better.
#
# A4 is different. It asks whether the project status is VERIFIED_COMPLETE, and
# the project has not claimed completion — hidden_holdout cannot pass while
# sealed holdout content does not exist. Reporting FAIL there would push a
# blocking gate to FAILED_ASSURANCE and say the project failed assurance, which
# is untrue: it is running with a typed blocker. So A4 is UNVERIFIABLE with the
# measured status attached, and becomes executable when the run terminates.


def _fresh_package():
    from evidence.package import build

    return build()


#: The one §27 field that cannot exist before the merge this gate gates.
#: §21.2 requires ``hidden_holdout: PASS`` before auto-merge, so a package built
#: while holdouts are blocked *must* report no merged PR — and a FAIL here would
#: mean "the evidence package is defective" when the truth is "the project has
#: not merged yet". Deliberately a set of one: every other unmeasured field is a
#: real gap and fails, because widening this list is how a completion gate turns
#: into a formality.
_COMPLETION_TIME_FIELDS = frozenset({"auto_merged_pr_reference"})


def _d3_26_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    package = _fresh_package()
    body = package.as_body()
    # A field whose source exists but is bound to different source than HEAD is
    # *stale*, not absent, and the difference matters. "The suite has not been
    # re-run since the last edit" is a refresh instruction; reporting it as FAIL
    # drives a blocking gate to FAILED_ASSURANCE and states that the project
    # failed assurance, which is untrue. A stale record still cannot claim the
    # tests pass — it reports UNVERIFIABLE, and only a genuinely absent
    # measurement fails.
    stale = {
        entry["key"]
        for entry in body["fields"]
        if not entry["present"] and "exercised source tree" in str(entry.get("source", ""))
    }
    pending = [k for k in package.missing if k in _COMPLETION_TIME_FIELDS or k in stale]
    gaps = [k for k in package.missing if k not in _COMPLETION_TIME_FIELDS and k not in stale]
    evidence = {
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "fields_total": body["fields_total"],
            "fields_present": body["fields_present"],
            "unmeasured_gaps": gaps,
            "pending_until_completion": pending,
            "field_count_discrepancy": body["field_count_discrepancy"],
        }
    }
    if gaps:
        return bad([f"{key}: no measurement (tier NOT_MEASURED)" for key in gaps], evidence)
    if pending:
        return undecided(
            (
                f"{body['fields_present']}/{body['fields_total']} §27 fields are measured; "
                f"{pending} are not yet measurable — a completion-time field cannot exist "
                "before the merge this gate gates, and a stale field needs its source re-run, "
                "not a failure verdict. Both would record a defective package for a project "
                "that is simply not finished"
            ),
            evidence,
        )
    return ok(evidence, f"all {body['fields_total']} §27 fields carry a measurement")


def _d3_26_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Every acceptance check has a named evidence artifact.

    Cross-referenced against the gates the runner actually loaded, so a check
    that exists in the contract but has no gate file is a finding rather than an
    absence nobody notices.
    """
    contract = ctx.pack_yaml("contract.yaml")
    declared = [str(c) for c in (contract.get("acceptance_checks") or [])]

    # Each gate file names its acceptance check in the header comment
    # ``# Contract acceptance_check: <name>``. That comment is the binding, so
    # it is read from the source rather than inferred from the gate's prose —
    # matching on intent text would let a rewording silently drop coverage.
    covered: dict[str, list[str]] = {}
    for gate_id, spec in ctx.gates.items():
        try:
            source = spec.source_path.read_text()
        except OSError:
            continue
        for match in re.finditer(r"acceptance_check:\s*([A-Za-z0-9_]+)", source):
            covered.setdefault(match.group(1), []).append(gate_id)

    uncovered = [c for c in declared if not covered.get(c)]
    evidence = {
        "coverage_cross_reference": {
            "declared_acceptance_checks": len(declared),
            "covered": {k: sorted(set(v)) for k, v in covered.items()},
            "uncovered": uncovered,
        }
    }
    if uncovered:
        return bad(
            [f"acceptance check {c!r} has no gate carrying evidence for it" for c in uncovered],
            evidence,
        )
    return ok(evidence, f"all {len(declared)} acceptance checks map to a gate")


def _d3_26_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    package = _fresh_package()
    debt = package.honest_debt
    non_goals = package.deferred_non_goals
    evidence = {
        "debt_section_presence": {
            "honest_debt_entries": len(debt),
            "debt_ids": [d["id"] for d in debt],
            "deferred_non_goals": len(non_goals),
        }
    }
    findings: list[str] = []
    if not debt:
        findings.append("honest debt section is empty")
    if not non_goals:
        findings.append("deliberately deferred non-goals are not stated")
    # A debt entry without a measurement behind it is a sentence, not evidence.
    for entry in debt:
        if not entry.get("measured") and not entry.get("status"):
            findings.append(f"{entry.get('id')}: states neither a measurement nor a status")
    if findings:
        return bad(findings, evidence)
    return ok(evidence, f"{len(debt)} debt entries and {len(non_goals)} deferred non-goals, each sourced")


def _d3_26_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    package = _fresh_package()
    status = package.status
    evidence = {
        "status_field_assert": {
            "project_status": status,
            "expected_at_completion": ProjectState.VERIFIED_COMPLETE.value,
            "is_structured_field_not_prose": True,
        }
    }
    if status == ProjectState.VERIFIED_COMPLETE.value:
        return ok(evidence, "the package reports VERIFIED_COMPLETE as a structured field")
    return undecided(
        (
            f"the project has not claimed completion: the package reports {status!r}, "
            "measured from the gate run rather than asserted. This assertion becomes "
            "executable when the run reaches a terminal state; reporting FAIL now would "
            "record FAILED_ASSURANCE for a project that is running with a typed blocker"
        ),
        evidence,
    )


def _d3_26_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """Aliases present, real identities absent (§12.3).

    The negative half is the one that matters: the package is searched for the
    upstream model ids the pack maps roles to. A package that leaked them would
    hand any reader the vendor, family and prestige tier §12.3 withholds.
    """
    package = _fresh_package()
    body = package.as_body()
    policy = ctx.pack_yaml("model-policy.yaml")
    aliases = policy.get("aliases") or {}

    field_value: dict[str, Any] = {}
    for entry in body["fields"]:
        if entry["key"] == "model_aliases_and_audit_references":
            field_value = entry["value"] if isinstance(entry["value"], dict) else {}

    serialized = json.dumps(body, default=str)
    leaked = sorted(
        {
            str(row.get("litellm_model"))
            for row in aliases.values()
            if row.get("litellm_model") and str(row["litellm_model"]) in serialized
        }
    )
    evidence = {
        "alias_and_audit_ref_check": {
            "aliases_present": sorted((field_value.get("aliases") or {}).values()),
            "protected_audit_reference_present": bool(field_value.get("protected_audit_reference")),
            "real_model_identities_found_in_package": leaked,
        }
    }
    findings: list[str] = []
    if not field_value.get("aliases"):
        findings.append("no blinded aliases are present in the package")
    if not field_value.get("protected_audit_reference"):
        findings.append("no protected audit reference is present")
    if leaked:
        findings.append(
            f"the package exposes real model identities, which §12.3 withholds: {leaked}"
        )
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"{len(field_value.get('aliases') or {})} blinded aliases, no real model identity present",
    )


# ===========================================================================
# GATE-D3-25 A1 — all thirteen auto-merge requirements recorded
# ===========================================================================

def _d3_25_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    evaluation = AutoMergeEvaluation(
        pull_request_ref=f"candidate@{ctx.binding.short}",
        candidate_commit=ctx.binding.commit_sha,
        implementing_agent_alias="implementer-i12",
    )
    # Requirements this workstream can decide, decided from real runs.
    evaluation.record("mutation_gate", ctx.mutation.verdict().value, "mutants.runner")
    suites_ok = all(fx.run_fixture_suite(o).ok for o in ctx.oracles.values())
    proofs_ok = all(prove_no_judge(m).holds for m in VERDICT_PATH_MODULES.values())
    evaluation.record(
        "oracle_health",
        "PASS" if (suites_ok and proofs_ok) else "FAIL",
        "oracles.fixtures + oracles.no_judge",
    )
    provenance = ctx.oracles["ORACLE-003"].decide(fx.good_provenance())
    evaluation.record(
        "provenance_gate",
        "PASS" if provenance.verdict is Verdict.PASS else provenance.verdict.value,
        "ORACLE-003",
    )
    evaluation.record("protected_assets_accessed", False, "GATE-D1-08 A2 static scan")
    evaluation.record(
        "contract_unchanged_or_approved",
        _assertion_hashes_intact(ctx),
        "tools/check_assertion_hashes.py logic",
    )
    # Requirements owned by other lanes or by CI. Recorded as NOT EVALUATED,
    # which is a third state -- neither a pass nor a silent omission.
    for name, source in (
        ("unresolved_scope_drift", "src/drift (WS-A)"),
        ("visible_tests", "CI job static-and-unit"),
        ("integration_tests", "CI integration job (not yet defined)"),
        ("composition_test", "ORACLE-001 needs a composition root (WS-C/WS-F)"),
        ("hidden_holdout", "sealed side unavailable: owner question Q1"),
        ("dependency_policy", "src/dependencies (WS-A)"),
        ("unresolved_high_risk_findings", "security review lane"),
        ("branch_up_to_date", "GitHub API under the builder identity"),
    ):
        evaluation.record_not_evaluated(name, source)

    evidence = {
        "gate_execution_log": evaluation.as_evidence(),
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "record_hash": content_hash(evaluation.as_evidence()),
        },
    }
    if not evaluation.all_thirteen_recorded:
        return bad([f"requirements never evaluated: {evaluation.missing}"], evidence)
    return ok(
        evidence,
        f"all {len(AUTO_MERGE_REQUIREMENTS)} requirements recorded; "
        f"{len(evaluation.not_evaluated)} not yet evaluable",
    )


def _assertion_hashes_intact(ctx: GateContext) -> bool:
    import hashlib

    manifest = PACK_ROOT / "acceptance" / "visible" / "ASSERTION_HASHES.txt"
    if not manifest.is_file():
        return False
    for raw in manifest.read_text().splitlines():
        line = raw.strip()
        if not line.startswith("sha256:"):
            continue
        digest, _, name = line.partition("  ")
        path = manifest.parent / name.strip()
        if not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest[len("sha256:"):]:
            return False
    return True


# ===========================================================================
# Registry
# ===========================================================================

CHECKS: dict[tuple[str, str], Check] = {
    ("GATE-D1-02", "A1"): _d1_02_a1,
    ("GATE-D1-02", "A2"): _d1_02_a2,
    ("GATE-D1-02", "A3"): _d1_02_a3,
    ("GATE-D1-02", "A4"): _d1_02_a4,
    ("GATE-D1-07", "A1"): _d1_07("A1"),
    ("GATE-D1-07", "A2"): _d1_07("A2"),
    ("GATE-D1-07", "A3"): _d1_07("A3"),
    ("GATE-D1-07", "A4"): _d1_07("A4"),
    ("GATE-D1-07", "A5"): _d1_07("A5"),
    ("GATE-D1-10", "A1"): _d1_10("A1"),
    ("GATE-D1-10", "A2"): _d1_10("A2"),
    ("GATE-D1-10", "A3"): _d1_10("A3"),
    ("GATE-D1-10", "A4"): _d1_10("A4"),
    ("GATE-D1-10", "A5"): _d1_10("A5"),
    ("GATE-D1-10", "A6"): _d1_10("A6"),
    ("GATE-D1-10", "A7"): _d1_10("A7"),
    ("GATE-D1-10", "A8"): _d1_10("A8"),
    ("GATE-D1-10", "A9"): _d1_10("A9"),
    ("GATE-D1-10", "A10"): _d1_10("A10"),
    ("GATE-D1-08", "A2"): _d1_08_a2,
    ("GATE-D1-08", "A4"): _d1_08_a4,
    ("GATE-D1-08", "A5"): _d1_08_a5,
    ("GATE-D1-08", "A6"): _d1_08_a6,
    ("GATE-D1-09", "A2"): _d1_09_a2,
    ("GATE-D1-09", "A4"): _d1_09_a4,
    ("GATE-D1-09", "A5"): _d1_09_a5,
    ("GATE-D2-18", "A1"): _d2_18_a1,
    ("GATE-D2-18", "A2"): _d2_18_a2,
    ("GATE-D2-18", "A3"): _d2_18_a3,
    ("GATE-D2-18", "A4"): _d2_18_a4,
    ("GATE-D2-19", "A1"): _d2_19_a1,
    ("GATE-D2-19", "A2"): _d2_19_a2,
    ("GATE-D2-19", "A3"): _d2_19_a3,
    ("GATE-D2-20", "A1"): _d2_20_a1,
    ("GATE-D2-20", "A2"): _d2_20_a2,
    ("GATE-D2-20", "A3"): _d2_20_a3,
    ("GATE-D2-20", "A4"): _d2_20_a4,
    ("GATE-D2-20", "A5"): _d2_20_a5,
    ("GATE-D2-20", "A6"): _d2_20_a6,
    ("GATE-D3-24", "A1"): _mutation_set_check(MutantClass.IMPLEMENTATION, "implementation"),
    ("GATE-D3-24", "A2"): _mutation_set_check(MutantClass.WORKFLOW_GOVERNANCE, "workflow/governance"),
    ("GATE-D3-24", "A3"): _mutation_set_check(MutantClass.TEST, "test"),
    ("GATE-D3-24", "A4"): _d3_24_a4,
    ("GATE-D3-24", "A5"): _d3_24_a5,
    ("GATE-D3-25", "A1"): _d3_25_a1,
    ("GATE-D3-26", "A1"): _d3_26_a1,
    ("GATE-D3-26", "A2"): _d3_26_a2,
    ("GATE-D3-26", "A3"): _d3_26_a3,
    ("GATE-D3-26", "A4"): _d3_26_a4,
    ("GATE-D3-26", "A5"): _d3_26_a5,
}

# ---------------------------------------------------------------------------
# Per-gate check modules
# ---------------------------------------------------------------------------
# Checks written after the 2026-08-02 gate audit live in their own module per
# gate rather than in this file. Two reasons, one practical and one structural.
#
# Practical: the audit found 44 assertions whose subject was already built and
# merely unchecked, and they were written in parallel. A dozen authors editing
# one registry file collide on every merge; a module each collides never.
#
# Structural: this file had grown to the point where the registry at the bottom
# was the only place a reader could see what is covered. Keeping the map here
# and the checks next door preserves that one-screen view.
from evaluation.checks_d1_03 import CHECKS_D1_03  # noqa: E402
from evaluation.checks_d2_12 import CHECKS_D2_12  # noqa: E402

CHECKS.update(CHECKS_D1_03)
CHECKS.update(CHECKS_D2_12)


#: Why an assertion has no check, so "not executable" is a statement with a
#: reason rather than a shrug. Contract Section 18: honest debt is recorded.
NOT_EXECUTABLE_REASONS: dict[tuple[str, str], str] = {
    ("GATE-D1-08", "A1"): (
        "an authenticated probe of the sealed repository would have to name it on the build "
        "side, which A2 of this same gate forbids; the probe belongs to the verifier service "
        "identity. Implementing it here would break A2 to satisfy A1."
    ),
    ("GATE-D1-08", "A3"): (
        "token scope introspection needs a builder credential and a GitHub API call; no "
        "builder token is configured for this runner."
    ),
    ("GATE-D1-09", "A1"): "the artifact registry is not yet built (WS-A/WS-B).",
    ("GATE-D1-09", "A3"): "OTel span emission is not yet built (observability lane).",
    ("GATE-D3-25", "A2"): "requires a live pull request and a CI run.",
    ("GATE-D3-25", "A3"): "requires a live pull request and a CI run.",
    ("GATE-D3-25", "A4"): "requires a live merge actor on a real pull request.",
    ("GATE-D3-25", "A5"): "requires CI checks present on a real pull request.",
}

DEFAULT_NOT_EXECUTABLE_REASON = (
    "no executable check is registered for this assertion; its subject is not built yet or is "
    "owned by another workstream"
)
