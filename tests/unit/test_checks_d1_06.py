"""GATE-D1-06's checks, and the proof that each of them can fail.

Contract Sections 11.2, 12.3 · Section 18. A blinding check that passes on the
real system says very little on its own: a scanner with an empty pattern list
reports zero matches too, and a store that reveals nothing to anybody satisfies
"only the owner may reveal it". So every check here is exercised twice -- once
against the real router, the real middleware, the real pack and the real
protected instance, and once against a subject broken in exactly one named way.

The broken subjects, one property each:

* a scanner that finds nothing (A1, A2, A5) -- the shape of a blinding gate
  somebody disabled, and the arm that would otherwise go green forever;
* a prompt carrying a real model id, and an adapter with no blinding call at
  all (A1) -- the payload and the enforcement point are separate claims;
* an orchestrator alias that is a model id, and a Principal that validates
  nothing (A2);
* a protected database configured at the main location, and a location
  predicate that calls everything separated (A3);
* an identity store that resolves for any caller, and a reveal that leaves no
  audit record (A4);
* a RoutingDecision that declares a cost tier, and an instrument that flags
  every payload including the clean one (A5).

Two of A4's arms need the owner's protected credential and a reachable
protected instance. Where they are absent the check reports UNVERIFIABLE by
design, and the tests that assert PASS skip with that reason rather than
lowering the bar to whatever this host can prove.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evaluation import checks_d1_06
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d1_06 import CHECKS_D1_06
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict
from models.blinding import PackIdentityStore

GATE_ID = "GATE-D1-06"
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "evaluation" / "checks_d1_06.py"


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context. The commit is a stand-in because these tests are about
    the checks; the gate-runner test at the end binds to the real HEAD."""
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D1_06[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


def evidence_of(outcome) -> dict[str, Any]:
    """The single artifact each check emits, whatever it is named."""
    assert len(outcome.evidence) == 1, outcome.evidence
    return next(iter(outcome.evidence.values()))


@pytest.fixture(scope="module")
def live_reveal_state(ctx: GateContext, gate: GateSpec) -> str:
    """Whether the owner reveal could actually be exercised on this host."""
    outcome = run(ctx, gate, "A4")
    return evidence_of(outcome)["live_reveal"]["state"]


def requires_the_protected_instance(state: str) -> None:
    if state != "revealed":
        pytest.skip(
            f"the owner reveal could not be exercised here (state={state!r}); GATE-D1-06 A4 "
            "reports UNVERIFIABLE rather than PASS, and so does this test"
        )


# --- broken subjects -------------------------------------------------------


def blind_scanner(*_args: Any, **_kwargs: Any) -> list:
    """A scanner that finds nothing. The shape of a disabled blinding gate."""
    return []


def permissive_gate(*_args: Any, **_kwargs: Any) -> None:
    """``assert_task_payload_blinded`` with the raise taken out."""
    return None


def flags_everything(*_args: Any, **_kwargs: Any) -> list[tuple[str, str]]:
    """The opposite failure: an instrument that rejects every payload."""
    return [("$", "flagged-unconditionally")]


class StoreThatResolvesForAnyone(PackIdentityStore):
    """A protected map with the caller check removed."""

    PRIVILEGED_CALLERS = frozenset(
        {"dispatch_service", "owner_audit", "availability_probe", "researcher-r17",
         "implementer-i12", "judge-j03", "anonymous"}
    )


class PrincipalThatValidatesNothing:
    """An api.context.Principal with its ``assert_alias_only`` guard dropped."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def audit_identity(self) -> dict[str, str]:
        return {"identity_kind": "alias", "subject": self.subject, "alias": self.alias}


# --- the registry ----------------------------------------------------------


def test_the_registry_covers_every_assertion_the_pack_declares(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D1_06 if gid == GATE_ID}
    assert registered == declared
    assert all(gid == GATE_ID for gid, _ in CHECKS_D1_06)


def test_this_module_does_not_import_evaluation_checks_at_module_scope():
    """The circular-import guard, asserted rather than remembered.

    ``checks.py`` imports this module to register it. An ``from
    evaluation.checks import ok`` at module scope here would make the pair
    circular, and which side breaks would depend on which module Python loads
    first -- so it would work under the gate runner and explode under pytest.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    module_scope_imports = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "evaluation.checks" not in module_scope_imports


def test_the_module_imports_cleanly_on_its_own_in_a_fresh_interpreter():
    """The same guard, measured instead of read off the source."""
    proc = subprocess.run(
        [sys.executable, "-c", "import evaluation.checks_d1_06 as m; print(len(m.CHECKS_D1_06))"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "5"


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_every_mapped_role(ctx: GateContext, gate: GateSpec):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = evidence_of(outcome)
    assert report["roles_scanned"] >= 15
    assert report["total_matches"] == 0
    assert all(
        record["accepted_by_assert_task_payload_blinded"]
        for record in report["per_role"].values()
    )
    assert report["enforcement_point"]["blinding_precedes_dispatch"]
    assert report["enforcement_point"]["scanned_keys"] == ["messages", "routing"]


def test_a1_fails_when_the_prompt_carries_a_real_model_id(ctx, gate, monkeypatch):
    policy = checks_d1_06.load_model_policy()
    leak = policy.role("implementer").litellm_model
    monkeypatch.setattr(
        checks_d1_06, "_NEAR_MISS_INSTRUCTIONS", f"You are running on {leak}. Write the test."
    )
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("vendor/model identity" in finding for finding in outcome.findings)


def test_a1_fails_when_the_scanner_finds_nothing(ctx, gate, monkeypatch):
    """The arm that matters: zero matches from an instrument that never matches."""
    monkeypatch.setattr(checks_d1_06, "scan_task_payload", blind_scanner)
    monkeypatch.setattr(checks_d1_06, "assert_task_payload_blinded", permissive_gate)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in finding for finding in outcome.findings)


def test_a1_fails_when_the_adapter_no_longer_scans_before_it_dispatches(ctx, gate, monkeypatch):
    """A clean payload proves nothing if nothing gates the real one."""
    monkeypatch.setattr(
        checks_d1_06, "_ADAPTER_PATH", Path("src") / "models" / "router.py"
    )
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not call assert_task_payload_blinded" in f for f in outcome.findings)


def test_a1_keeps_the_near_miss_words_in_the_clean_prompt(ctx, gate):
    """A pass that avoided 'code' and 'max' would be a pass about the probe."""
    report = evidence_of(run(ctx, gate, "A1"))
    assert report["clean_prompt_contains_near_miss_words"] == ["code", "max", "flash"]


# --- A2 --------------------------------------------------------------------


def test_a2_passes_on_a_real_event_stream_and_a_real_audit_record(ctx, gate):
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = evidence_of(outcome)
    stream = report["task_event_stream"]
    assert stream["events"] == 7
    assert stream["dispatch_boundary_findings"] == []
    assert stream["record_surface_findings"] == []
    record = report["audit_record"]["record"]
    assert record["alias"] in stream["mapped_aliases_referenced"]
    assert "authorization" not in record["headers_present"]
    assert report["audit_record"]["redacted_headers_absent"]


def test_a2_fails_when_an_actor_alias_is_a_real_model_id(ctx, gate, monkeypatch):
    policy = checks_d1_06.load_model_policy()
    monkeypatch.setattr(
        checks_d1_06, "_ORCHESTRATOR_ALIAS", policy.role("implementer").litellm_model
    )
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("task event stream" in finding for finding in outcome.findings)


def test_a2_fails_when_the_record_scanners_find_nothing(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_06, "scan_task_payload", blind_scanner)
    monkeypatch.setattr(checks_d1_06, "scan_for_leaks", blind_scanner)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in finding for finding in outcome.findings)


def test_a2_fails_when_a_principal_accepts_a_real_model_id_as_its_alias(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_06, "Principal", PrincipalThatValidatesNothing)
    outcome = run(ctx, gate, "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert any("a Principal was constructed with a real model id" in f for f in outcome.findings)


def test_a2_states_that_no_terminusdb_write_was_exercised(ctx, gate):
    """The honest limit is part of the evidence, not a comment someone can drop."""
    report = evidence_of(run(ctx, gate, "A2"))
    assert "No TerminusDB write occurs" in report["how_the_stream_is_produced"]


# --- A3 --------------------------------------------------------------------


def test_a3_passes_and_agrees_with_the_owner_declaration(ctx, gate):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    proof = evidence_of(outcome)
    assert proof["distinct_database"] and proof["distinct_endpoint"]
    assert proof["mapping_database"] != proof["main_project_database"]
    declared = proof["owner_declaration"]
    assert declared["isolated_instance"] is True
    assert declared["main_holds_protected_identity_mappings"] is False
    assert declared["main_admin_credential_must_fail"] is True
    assert proof["credential_declaration"]["used_by"] == ["owner_audit_path"]


def test_a3_holds_no_endpoint_literal_of_its_own():
    """Section 11.2 permits one module to hold a route. This is not that module."""
    source = MODULE_PATH.read_text()
    assert "PROTECTED_ENDPOINT" in source, "the constant must be imported, not inlined"
    assert "localhost:6364" not in source


def test_a3_fails_when_the_mapping_sits_at_the_main_location(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_06, "PROTECTED_DATABASE", checks_d1_06.DEFAULT_DATABASE)
    monkeypatch.setattr(checks_d1_06, "PROTECTED_ENDPOINT", checks_d1_06.DEFAULT_ENDPOINT)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("same name" in finding for finding in outcome.findings)
    assert any("share an endpoint" in finding for finding in outcome.findings)


def test_a3_fails_when_the_location_predicate_calls_everything_separated(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d1_06,
        "_locations_distinct",
        lambda **_kwargs: {
            "distinct_database": True,
            "distinct_endpoint": True,
            "separated": True,
        },
    )
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in finding for finding in outcome.findings)


def test_a3_fails_when_the_main_credential_reaches_the_protected_instance(
    ctx, gate, monkeypatch
):
    """A 200 there is a hard failure, and must never be repaired by granting access."""
    from integrations.protected_identity import IsolationProbeResult

    async def accepted(*_args: Any, **kwargs: Any) -> IsolationProbeResult:
        return IsolationProbeResult(
            endpoint="http://protected.invalid",
            actor=str(kwargs.get("actor", "builder/main-admin")),
            status=200,
            api_error_type=None,
            probed_at="2026-08-02T00:00:00+00:00",
        )

    monkeypatch.setenv("TERMINUSDB_ADMIN_PASS", "a-builder-credential")
    monkeypatch.setattr(checks_d1_06, "probe_credential_against_protected", accepted)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("must NOT be repaired by granting access" in f for f in outcome.findings)


# --- A4 --------------------------------------------------------------------


def test_a4_passes_when_the_owner_audit_path_is_reachable(ctx, gate, live_reveal_state):
    requires_the_protected_instance(live_reveal_state)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    transcript = evidence_of(outcome)
    live = transcript["live_reveal"]
    assert live["revealed_matches_pack_mapping"]
    assert live["alias_view_carries_no_identity"]
    assert live["audit_records_after"] == live["audit_records_before"] + 1
    assert live["audit_record"]["owner_identity"] == "owner_audit_path"
    assert live["audit_record"]["reason"]


def test_a4_offline_arms_refuse_every_caller_that_is_not_the_owner(ctx, gate):
    """This half runs everywhere, including a host with no protected instance."""
    transcript = evidence_of(run(ctx, gate, "A4"))
    refusals = transcript["offline_arms"]["dispatch_side_store"][
        "unprivileged_callers_refused"
    ]
    assert refusals and all(record["refused"] for record in refusals.values())
    assert transcript["offline_arms"]["dispatch_side_store"]["resolved_matches_pack_mapping"]
    assert all(
        record["refused"]
        for record in transcript["offline_arms"]["audit_context_required"].values()
    )
    assert transcript["offline_arms"]["absent_credential"]["refused"]
    assert (
        transcript["offline_arms"]["absent_credential"]["typed_blocker"]
        == "MISSING_REQUIRED_CREDENTIAL"
    )


def test_a4_reveals_nothing_it_was_told(ctx, gate):
    """The transcript must not contain the identity the reveal returned."""
    policy = checks_d1_06.load_model_policy()
    row = policy.role("implementer")
    rendered = repr(evidence_of(run(ctx, gate, "A4")))
    assert row.litellm_model not in rendered
    assert row.family not in rendered


def test_a4_fails_when_the_store_resolves_for_any_caller(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_06, "PackIdentityStore", StoreThatResolvesForAnyone)
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("without owner authority" in finding for finding in outcome.findings)


def test_a4_fails_when_a_reveal_leaves_no_audit_record(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d1_06,
        "_live_reveal",
        lambda *_args, **_kwargs: {
            "state": "revealed",
            "revealed_matches_pack_mapping": True,
            "alias_view_carries_no_identity": True,
            "audit_records_before": 3,
            "audit_records_after": 3,
            "audit_record": {"owner_identity": "owner_audit_path", "reason": "probe"},
        },
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("left no audit record" in finding for finding in outcome.findings)


def test_a4_fails_when_the_reveal_resolves_to_a_different_mapping(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d1_06,
        "_live_reveal",
        lambda *_args, **_kwargs: {
            "state": "revealed",
            "revealed_matches_pack_mapping": False,
            "alias_view_carries_no_identity": True,
            "audit_records_before": 0,
            "audit_records_after": 1,
            "audit_record": {"owner_identity": "owner_audit_path", "reason": "probe"},
        },
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not match the pack's mapping" in finding for finding in outcome.findings)


def test_a4_is_unverifiable_rather_than_green_without_the_credential(ctx, gate, monkeypatch):
    """The rule that keeps this gate honest on a host with no protected store."""
    monkeypatch.setattr(
        checks_d1_06,
        "_live_reveal",
        lambda *_args, **_kwargs: {
            "state": "no_credential",
            "detail": "the credential is not in this process's environment.",
        },
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.UNVERIFIABLE
    assert "a refusal to reveal is not a reveal" in outcome.findings[0]
    assert evidence_of(outcome)["offline_arms"]["absent_credential"]["refused"]


def test_a4_is_unverifiable_when_the_protected_credential_is_refused(ctx, gate, monkeypatch):
    monkeypatch.setattr(
        checks_d1_06,
        "_live_reveal",
        lambda *_args, **_kwargs: {"state": "unauthenticated", "detail": "HTTP 401."},
    )
    outcome = run(ctx, gate, "A4")
    assert outcome.status is AssertionStatus.UNVERIFIABLE
    assert "configuration finding" in outcome.findings[0]


# --- A5 --------------------------------------------------------------------


def test_a5_passes_with_no_ranking_field_anywhere(ctx, gate):
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    report = evidence_of(outcome)
    assert report["total_matches"] == 0
    assert report["structural_guarantees"]["routing_decision_carries_no_ranking_field"]
    assert set(report["structural_guarantees"]["role_row_blinded_projection"]) == {
        "role",
        "alias",
        "gateway",
    }
    assert all(record["detected"] for record in report["negative_control"]["injections"].values())
    assert report["negative_control"]["baseline_scans_clean"]


def test_a5_injects_every_ranking_field_not_just_a_sample(ctx, gate):
    report = evidence_of(run(ctx, gate, "A5"))
    assert set(report["negative_control"]["injections"]) == set(report["fields_searched_for"])
    coverage = report["detector_coverage"]
    # The two enforced lists differ, and the evidence must say which is which
    # rather than crediting the dispatch gate with names it does not know.
    assert coverage["caught_by_dispatch_boundary"]
    assert coverage["caught_only_by_record_surface"]
    assert not set(coverage["caught_by_dispatch_boundary"]) & set(
        coverage["caught_only_by_record_surface"]
    )


def test_a5_fails_when_a_payload_actually_carries_a_cost_tier(ctx, gate, monkeypatch):
    original = checks_d1_06._worker_payload

    def leaking(router, policy, role):
        decision, payload = original(router, policy, role)
        payload["routing"]["cost_tier"] = "frontier"
        return decision, payload

    monkeypatch.setattr(checks_d1_06, "_worker_payload", leaking)
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("carries ranking field 'cost_tier'" in finding for finding in outcome.findings)


def test_a5_fails_when_the_decision_type_could_carry_a_ranking_field(ctx, gate, monkeypatch):
    """Structural, not incidental: the type must not be able to hold one."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class DecisionWithATier:
        role: str = ""
        alias: str = ""
        cost_tier: str = ""

    monkeypatch.setattr(checks_d1_06, "RoutingDecision", DecisionWithATier)
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("RoutingDecision declares ranking" in finding for finding in outcome.findings)


def test_a5_fails_when_the_scanners_find_nothing(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_06, "scan_task_payload", blind_scanner)
    monkeypatch.setattr(checks_d1_06, "scan_for_leaks", blind_scanner)
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in finding for finding in outcome.findings)


def test_a5_fails_when_the_instrument_flags_the_clean_payload_too(ctx, gate, monkeypatch):
    """An instrument that rejects everything detects every injection for free."""
    monkeypatch.setattr(checks_d1_06, "scan_for_leaks", flags_everything)
    outcome = run(ctx, gate, "A5")
    assert outcome.status is AssertionStatus.FAIL
    assert any("does not scan clean" in finding for finding in outcome.findings)


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4", "A5"])
def test_every_check_binds_its_transcript_to_the_candidate_commit(ctx, gate, assertion_id):
    binding = evidence_of(run(ctx, gate, assertion_id))["binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", ["A1", "A2", "A3", "A4", "A5"])
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = evidence_of(run(ctx, gate, assertion_id))["negative_control"]
    assert control["probe"]
    assert control["why"]


def test_the_five_checks_together_produce_every_artifact_the_gate_named(ctx, gate):
    produced: set[str] = set()
    for assertion_id in ("A1", "A2", "A3", "A4", "A5"):
        produced |= set(run(ctx, gate, assertion_id).evidence)
    assert set(gate.evidence_required) <= produced


def test_the_registered_gate_runs_green_with_its_evidence(monkeypatch, live_reveal_state):
    """The registration entries, exercised end to end through the runner.

    This is what merging :data:`CHECKS_D1_06` into ``CHECKS`` buys: the gate
    reports EXECUTED instead of NOT_YET_EXECUTABLE, and it produces every
    artifact its own definition named.
    """
    requires_the_protected_instance(live_reveal_state)
    for key, check in CHECKS_D1_06.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.PASS, [a.findings for a in result.failed]
    assert result.evidence_missing == []
    assert result.executed_count == len(result.assertions) == 5


def test_the_gate_is_unverifiable_when_an_assertion_cannot_be_measured(monkeypatch):
    """A blocking gate must not report PASS on an assertion nobody could run."""
    monkeypatch.setattr(
        checks_d1_06,
        "_live_reveal",
        lambda *_args, **_kwargs: {"state": "unreachable", "detail": "no instance here."},
    )
    for key, check in CHECKS_D1_06.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]
    assert result.executability is Executability.EXECUTED
    assert result.verdict is Verdict.UNVERIFIABLE
    assert result.evidence_missing == []
