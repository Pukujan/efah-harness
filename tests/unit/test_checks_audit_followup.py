"""The six audit-follow-up checks, and the proof that each of them can fail.

Contract Section 18. Every check in :mod:`evaluation.checks_audit_followup` is
run twice here: once against the real subject, and once against a subject in
which the property the assertion names is false. The second run is what gives
the first its meaning, and each broken subject is broken in exactly one way,
named:

* a span gate that stamps the correlation attributes but never requires them,
  and a span-opener scan pointed at the wrong module (GATE-D1-09 A3),
* a composition verifier that never reports a finding, and an oracle that
  passes everything (GATE-D2-10 A1),
* an architecture suite that collected no tests, a cycle detector that finds
  none, and an import scanner with an empty prohibited list (GATE-D2-10 A4),
* a drift engine that guards no components, and one that flags every task
  (GATE-D2-13 A2),
* a compiled graph in which a task really does depend on a prohibited
  component, and a prohibited-node scan that looks at nothing (GATE-D2-13 A4),
* an interrupt gate that accepts any reason, a call-site scanner that sees
  offenders everywhere, and one that sees nothing at all (GATE-D3-23 A4).

The failure is asserted by *message*, not merely by status: a check that fails
for the wrong reason is a check that has stopped measuring its assertion.
"""

from __future__ import annotations

import ast
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from composition.registry import ModuleRegistry
from composition.root import build_registry
from drift.engine import DriftEngine, DriftReport, Finding
from evaluation import checks_audit_followup as followup
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_audit_followup import CHECKS_AUDIT_FOLLOWUP
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import DriftFinding, OwnerInterrupt, Verdict
from observability.spans import format_trace_id, tracer
from oracles.base import Decision, DeterministicOracle

#: Every ``(gate, assertion)`` this module owns, and the evidence each one is
#: expected to produce. GATE-D1-09's other three artifacts belong to A1, A2 and
#: A5; A3 owns the span sample and its own control transcript.
OWNED: dict[tuple[str, str], tuple[str, ...]] = {
    ("GATE-D1-09", "A3"): ("otel_span_sample", "negative_control_transcript"),
    ("GATE-D2-10", "A1"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
    # A2 answers the reachability question against the real import graph rather
    # than against the registry's own ``consumes`` column, and is staged to
    # UNVERIFIABLE while the owner reads the debt it enumerates.
    ("GATE-D2-10", "A2"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
    ("GATE-D2-10", "A4"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
    ("GATE-D2-13", "A2"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
    ("GATE-D2-13", "A4"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
    ("GATE-D3-23", "A4"): (
        "gate_execution_log",
        "negative_control_transcript",
        "artifact_hashes_and_commit_binding",
    ),
}

#: What is still not executable in each gate these six touch. Recorded here so
#: the remaining debt is a fact somebody has to edit rather than a silence.
STILL_UNIMPLEMENTED: dict[str, set[str]] = {
    "GATE-D1-09": {"A1"},
    # A2 left this set when the reachability check landed. A3
    # (``wiring_manifest_assert``) is still unwritten: the nine Section 5.2
    # fields are generated from f-strings in ``composition/root._declare``, so
    # ``project-pack/artifacts`` and ``tests/integration/#artifacts`` are
    # plausible strings that resolve to nothing, and ``is_placeholder`` only
    # rejects literal TODO markers. Checking it means resolving each field, not
    # pattern-matching it.
    "GATE-D2-10": {"A3"},
    "GATE-D2-13": {"A1", "A3"},
    "GATE-D3-23": {"A1", "A2", "A3"},
}


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    """A real context. The candidate commit is a stand-in; the runner test uses HEAD."""
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gates: dict[str, GateSpec], gate_id: str, assertion_id: str):
    gate = gates[gate_id]
    return CHECKS_AUDIT_FOLLOWUP[(gate_id, assertion_id)](ctx, gate, assertion(gate, assertion_id))


def findings_mentioning(outcome: Any, fragment: str) -> list[str]:
    return [f for f in outcome.findings if fragment in f]


# --- the registry and the circular-import rule -----------------------------


def test_the_registry_holds_exactly_the_audited_assertions():
    assert set(CHECKS_AUDIT_FOLLOWUP) == set(OWNED)


def test_every_registered_key_names_a_real_assertion_in_the_pack(gates):
    for gate_id, assertion_id in CHECKS_AUDIT_FOLLOWUP:
        gate = gates[gate_id]
        assert assertion_id in {a.assertion_id for a in gate.assertions}


def test_this_module_never_imports_checks_at_module_scope():
    """The cycle that would make the same code work in one entry point and not another.

    ``checks.py`` imports this module to register these six. Importing it back
    at module scope makes which side fails depend on which one Python loads
    first, so a gate run would work and pytest would explode -- or the reverse.
    ``ok`` and ``bad`` are resolved inside the wrappers instead, and this test
    is what keeps a later edit from quietly adding the import back.
    """
    source = Path(followup.__file__).read_text()
    tree = ast.parse(source)
    offenders = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("evaluation.checks")
    ]
    assert offenders == [], f"module-scope import of {offenders} closes the registration cycle"


# --- GATE-D1-09 A3 ---------------------------------------------------------


@contextmanager
def _span_gate_that_never_requires(name, *, kind, correlation, attributes=None):
    """Stamps every attribute it is given and demands nothing.

    The plausible half-implementation: the renderer is right, so a fully
    correlated span looks perfect, and a span missing ``run_id`` is opened and
    exported anyway. A3's first arm cannot tell the difference; its second arm
    must.
    """
    span_attributes = dict(correlation.attributes())
    span_attributes["efah.span_kind"] = str(kind)
    with tracer().start_as_current_span(name, attributes=span_attributes) as span:
        span.set_attribute("efah.trace_id", format_trace_id(span))
        yield span


def test_d1_09_a3_passes_against_the_real_span_module(ctx, gates):
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    sample = outcome.evidence["otel_span_sample"]
    assert sample["span_openers_outside_the_correlation_gate"]["offenders"] == []
    assert len(sample["fully_correlated_spans"]) == 8
    assert all(s["missing_correlation_fields"] == [] for s in sample["fully_correlated_spans"])


def test_d1_09_a3_records_the_eleven_versus_twelve_arity_rather_than_hiding_it(ctx, gates):
    fields = run(ctx, gates, "GATE-D1-09", "A3").evidence["otel_span_sample"][
        "minimum_correlation_fields"
    ]
    assert fields["assertion_claims"] == 11
    assert len(fields["supplied_by_a_caller"]) == 11
    assert fields["module_constant_count"] == 12
    assert fields["derived_from_the_live_span_context"] == "trace_id"
    assert "never accepted as an input" in fields["why_the_counts_differ"]


def test_d1_09_a3_drives_two_live_emitters_on_one_trace(ctx, gates):
    """The API request and the project import are separate emitters, one trace."""
    live = run(ctx, gates, "GATE-D1-09", "A3").evidence["otel_span_sample"]["live_emitter_spans"]
    assert {record["kind"] for record in live} == {"api_request", "project"}
    assert len({record["trace_id"] for record in live}) == 1
    assert all(record["minimum_fields_absent"] == [] for record in live)


def test_d1_09_a3_fails_when_the_correlation_is_stamped_but_not_required(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "efah_span", _span_gate_that_never_requires)
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "cannot be joined to the run it came from")


def test_d1_09_a3_fails_when_a_span_is_opened_outside_the_correlation_gate(ctx, gates, monkeypatch):
    """Point the exclusion at the wrong module and spans.py becomes the offender.

    This is the arm that makes 'every emitted span' a closed claim rather than
    a claim about the spans that happen to go through ``efah_span``: it shows
    the scan really does find the span opener, and would report it from any
    other module.
    """
    monkeypatch.setattr(followup, "CORRELATION_GATE_MODULE", Path("observability") / "identity.py")
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "outside the Section 23 correlation gate")


def test_d1_09_a3_fails_when_the_opener_scanner_is_blind(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "SPAN_OPENING_CALLS", frozenset({"open_a_span_somehow"}))
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "negative control did not fire")


def test_d1_09_a3_fails_when_the_live_emitters_emit_nothing(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "_live_emitter_spans", lambda repo_root, exporter: [])
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "two distinct emitters and both must appear")


def test_d1_09_a3_fails_when_a_correlation_field_is_added_without_the_contract(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "ASSERTION_FIELD_COUNT", 12)
    outcome = run(ctx, gates, "GATE-D1-09", "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "minimum correlation fields but the module supplies")


# --- GATE-D2-10 A1 ---------------------------------------------------------


class _VerifierThatNeverFails(ModuleRegistry):
    """A composition verifier with the rejection removed."""

    def verify(self, *, entrypoints: set[str]) -> list[Any]:
        return []


class _OracleThatPassesEverything(DeterministicOracle):
    @property
    def oracle_id(self) -> str:
        return "ORACLE-001"

    def decide(self, subject: Any) -> Decision:
        return Decision(verdict=Verdict.PASS, reasons=["stub: everything is wired"])


def _permissive_registry() -> ModuleRegistry:
    real = build_registry()
    stub = _VerifierThatNeverFails(root_provides=set(real.root_provides))
    stub.declarations.update(real.declarations)
    return stub


def test_d2_10_a1_passes_against_the_real_composition_root(ctx, gates):
    outcome = run(ctx, gates, "GATE-D2-10", "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["pack_fixture"]["verdict"] == Verdict.FAIL.value
    assert log["pack_fixture"]["failure_state"] == "FAILED_WIRING"
    assert log["live_composition_root"]["verdict"] == Verdict.PASS.value
    assert log["live_unregistration"]["failure_state"] == "FAILED_WIRING"
    assert log["live_declaration_deleted"]["findings"]


def test_d2_10_a1_names_the_unregistered_module_on_live_data(ctx, gates):
    log = run(ctx, gates, "GATE-D2-10", "A1").evidence["gate_execution_log"]
    assert any(
        followup.UNREGISTERED_PROBE_MODULE in reason
        for reason in log["live_unregistration"]["reasons"]
    )
    assert all(f["kind"] == "MISSING_WIRING" for f in log["live_declaration_deleted"]["findings"])


def test_d2_10_a1_fails_when_the_verifier_never_reports_a_finding(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "build_registry", _permissive_registry)
    outcome = run(ctx, gates, "GATE-D2-10", "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "produced no composition finding at all")


def test_d2_10_a1_fails_when_the_oracle_passes_everything(gates):
    context = GateContext(
        binding=CandidateBinding(commit_sha="b" * 40),
        gates=gates,
        _oracles={
            "ORACLE-001": _OracleThatPassesEverything(
                {
                    "oracle_id": "ORACLE-001",
                    "model_call_in_verdict_path": False,
                    "judge_participates": False,
                }
            )
        },
    )
    outcome = run(context, gates, "GATE-D2-10", "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "produced PASS, not FAIL")
    assert findings_mentioning(outcome, "not FAILED_WIRING")


# --- GATE-D2-10 A4 ---------------------------------------------------------


class _CycleBlindRegistry(ModuleRegistry):
    def cycles(self) -> list[Any]:
        return []


def _cycle_blind_registry() -> ModuleRegistry:
    real = build_registry()
    stub = _CycleBlindRegistry(root_provides=set(real.root_provides))
    stub.declarations.update(real.declarations)
    return stub


def test_d2_10_a4_passes_against_the_pinned_architecture_suite(ctx, gates):
    outcome = run(ctx, gates, "GATE-D2-10", "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["architecture_test_run"]["returncode"] == 0
    assert log["architecture_test_run"]["tests_passed"] > 0
    assert log["cycle_scan"]["cycles"] == []
    assert log["prohibited_import_scan"]["offenders"] == []


def test_d2_10_a4_shows_both_detectors_firing(ctx, gates):
    control = run(ctx, gates, "GATE-D2-10", "A4").evidence["negative_control_transcript"]
    assert control["injected_cycles"]
    assert all(f["kind"] == "CIRCULAR_DEPENDENCY" for f in control["injected_cycles"])
    assert control["planted_prohibited_imports"] == ["anthropic", "temporalio"]


def test_d2_10_a4_fails_when_the_suite_collected_nothing(ctx, gates, monkeypatch):
    """A green exit status over an empty collection is the vacuous pass this guards."""

    def collected_nothing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="no tests ran\n", stderr="")

    monkeypatch.setattr(followup.subprocess, "run", collected_nothing)
    outcome = run(ctx, gates, "GATE-D2-10", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "collected nothing has zero violations for the wrong reason")


def test_d2_10_a4_fails_when_the_suite_fails(ctx, gates, monkeypatch):
    def red(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="1 failed, 27 passed\n", stderr=""
        )

    monkeypatch.setattr(followup.subprocess, "run", red)
    outcome = run(ctx, gates, "GATE-D2-10", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "1 failed")


def test_d2_10_a4_fails_when_the_cycle_detector_finds_nothing(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "build_registry", _cycle_blind_registry)
    outcome = run(ctx, gates, "GATE-D2-10", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "produced no CIRCULAR_DEPENDENCY")


def test_d2_10_a4_fails_when_the_import_scanner_prohibits_nothing(ctx, gates, monkeypatch):
    class _EmptyPolicyScanner:
        PROHIBITED: frozenset[str] = frozenset()

        @staticmethod
        def _module_imports(path: Path) -> set[str]:
            return set()

    monkeypatch.setattr(followup, "_load_pinned_module", lambda path, name: _EmptyPolicyScanner)
    outcome = run(ctx, gates, "GATE-D2-10", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "imports two prohibited packages")


# --- GATE-D2-13 A2 ---------------------------------------------------------


class _EngineThatGuardsNothing(DriftEngine):
    """The prohibited list emptied: every reimplementation looks like new work."""

    def _prohibited_components(self) -> set[str]:
        return set()


class _EngineThatFlagsEveryTask(DriftEngine):
    """A detector with the condition removed. It 'catches' the injection too."""

    def scan(self, scan_input: Any) -> DriftReport:
        report = super().scan(scan_input)
        report.findings.append(
            Finding(
                finding=str(DriftFinding.UNSUPPORTED_REIMPLEMENTATION),
                subject="everything",
                detail="stub: flagged without looking",
            )
        )
        return report


def test_d2_13_a2_passes_against_the_real_drift_engine(ctx, gates):
    outcome = run(ctx, gates, "GATE-D2-13", "A2")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["prohibited_components"] == log["prohibited_components_the_engine_derived"]
    assert len(log["injections"]) == len(log["prohibited_components"]) == 8
    assert all(entry["unsupported_reimplementation_count"] == 1 for entry in log["injections"])
    assert all(entry["blocks"] and entry["names_the_component"] for entry in log["injections"])
    assert all(entry["terminal_state"] == "FAILED_CONTRACT" for entry in log["injections"])


def test_d2_13_a2_control_arms_are_all_clean(ctx, gates):
    control = run(ctx, gates, "GATE-D2-13", "A2").evidence["negative_control_transcript"]
    assert control["clean_task"]["findings"] == []
    assert control["introduction_with_a_recorded_decision"]["findings"] == []
    assert control["introduction_that_duplicates_nothing"]["findings"] == []


def test_d2_13_a2_fails_when_the_engine_guards_no_components(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "DriftEngine", _EngineThatGuardsNothing)
    outcome = run(ctx, gates, "GATE-D2-13", "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "scored against the wrong list")
    assert findings_mentioning(outcome, "findings, not 1")


def test_d2_13_a2_fails_when_the_engine_flags_every_task(ctx, gates, monkeypatch):
    """An engine that rejects everything 'rejects the injected duplicate' too."""
    monkeypatch.setattr(followup, "DriftEngine", _EngineThatFlagsEveryTask)
    outcome = run(ctx, gates, "GATE-D2-13", "A2")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "negative control failed")
    assert findings_mentioning(outcome, "duplicates nothing was rejected")


# --- GATE-D2-13 A4 ---------------------------------------------------------


def test_d2_13_a4_passes_against_the_real_dependency_graph(ctx, gates):
    outcome = run(ctx, gates, "GATE-D2-13", "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["prohibited_by_the_owner_policy"] == [
        node.removeprefix("PKG:") for node in sorted(log["prohibited_nodes_in_the_compiled_graph"])
    ]
    assert all(entry["integration_edges"] == [] for entry in log["per_prohibited_component"])
    assert log["emitted_integration_matches"] == []


def test_d2_13_a4_treats_the_owners_exclusion_record_as_an_exclusion(ctx, gates):
    """The prohibited nodes are present on purpose; their conflicts_with edges say why."""
    log = run(ctx, gates, "GATE-D2-13", "A4").evidence["gate_execution_log"]
    with_exclusions = [e for e in log["per_prohibited_component"] if e["declared_exclusion_edges"]]
    assert with_exclusions, "no prohibited component records what it duplicates"
    assert all(
        edge["edge_type"] == followup.DECLARED_EXCLUSION_EDGE
        for entry in with_exclusions
        for edge in entry["declared_exclusion_edges"]
    )


def test_d2_13_a4_fails_when_a_task_really_depends_on_a_prohibited_component(ctx, gates, monkeypatch):
    """The property made false in the graph itself, not in the scanner."""
    vandalised = followup._disposable_compilation(ctx.repo_root)
    task = sorted(vandalised.graph.nodes_of_kind("Task"))[0]
    vandalised.graph.add_edge(
        task,
        "PKG:temporal",
        "depends_on",
        dependency_class="software_package",
        rationale="test: an integration the contract forbids",
    )
    monkeypatch.setattr(followup, "_compiled", lambda repo_root: vandalised)
    outcome = run(ctx, gates, "GATE-D2-13", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "'temporal' is on an integration edge")


def test_d2_13_a4_fails_when_the_scan_looks_at_no_prohibited_nodes(ctx, gates, monkeypatch):
    """Zero matches over an empty search is the vacuous pass this guards."""
    monkeypatch.setattr(followup, "_prohibited_nodes", lambda project: {})
    outcome = run(ctx, gates, "GATE-D2-13", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "a statement about an empty search")


def test_d2_13_a4_fails_when_conflicts_with_stops_being_the_exclusion_edge(ctx, gates, monkeypatch):
    """If the excused edge type is wrong, the owner's own records read as integrations."""
    monkeypatch.setattr(followup, "DECLARED_EXCLUSION_EDGE", "supersedes")
    outcome = run(ctx, gates, "GATE-D2-13", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "is on an integration edge")


# --- GATE-D3-23 A4 ---------------------------------------------------------


class _ScannerSeeingOffendersEverywhere:
    @staticmethod
    def _calls_langgraph_interrupt(path: Path) -> bool:
        return True


class _ScannerSeeingNothing:
    @staticmethod
    def _calls_langgraph_interrupt(path: Path) -> bool:
        return False


def test_d3_23_a4_passes_against_the_real_interrupt_gate(ctx, gates):
    outcome = run(ctx, gates, "GATE-D3-23", "A4")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    log = outcome.evidence["gate_execution_log"]
    assert log["call_site_scan"]["offenders"] == []
    assert log["closed_interrupt_type_set"] == log["autonomy_policy_human_interrupts_only"]
    assert len(log["prohibited_reason_refusals"]) == 9
    assert all(r["raised"] == "IllegalInterrupt" and r["diagnosed"] for r in log["prohibited_reason_refusals"])
    assert len(log["permitted_reasons_accepted"]) == len(list(OwnerInterrupt))


def test_d3_23_a4_audits_the_recorded_interrupt_types(ctx, gates):
    log = run(ctx, gates, "GATE-D3-23", "A4").evidence["gate_execution_log"]
    assert log["recorded_interrupt_types"] == log["closed_interrupt_type_set"]
    assert len(log["recorded_escalation_conditions"]) == 7
    assert all(record["rejected_on_interrupt_type"] for record in log["recorded_blocker_types"])
    assert all(
        not record["permitted_value_rejected_on_interrupt_type"]
        for record in log["recorded_blocker_types"]
    )


def test_d3_23_a4_stops_a_forged_reason_at_the_only_call_site(ctx, gates):
    control = run(ctx, gates, "GATE-D3-23", "A4").evidence["negative_control_transcript"]
    assert control["forged_reason_at_the_only_call_site"]["raised"] == "IllegalInterrupt"
    assert control["forged_reason_at_the_only_call_site"]["reached_langgraph"] is False


def test_d3_23_a4_fails_when_the_interrupt_gate_accepts_any_reason(ctx, gates, monkeypatch):
    monkeypatch.setattr(
        followup, "coerce_reason", lambda reason: OwnerInterrupt.OWNER_SCOPE_DECISION
    )
    outcome = run(ctx, gates, "GATE-D3-23", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "must be refused as an IllegalInterrupt")


def test_d3_23_a4_fails_when_the_forged_reason_reaches_langgraph(ctx, gates, monkeypatch):
    monkeypatch.setattr(followup, "owner_interrupt", lambda request: None)
    outcome = run(ctx, gates, "GATE-D3-23", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "forged past pydantic")


def test_d3_23_a4_fails_when_a_second_call_site_exists(ctx, gates, monkeypatch):
    monkeypatch.setattr(
        followup, "_load_pinned_module", lambda path, name: _ScannerSeeingOffendersEverywhere
    )
    outcome = run(ctx, gates, "GATE-D3-23", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "outside the Section 10.7 gate")


def test_d3_23_a4_fails_when_the_call_site_scanner_sees_nothing(ctx, gates, monkeypatch):
    """A scanner that detects no call site reports zero offenders for free."""
    monkeypatch.setattr(followup, "_load_pinned_module", lambda path, name: _ScannerSeeingNothing)
    outcome = run(ctx, gates, "GATE-D3-23", "A4")
    assert outcome.status is AssertionStatus.FAIL
    assert findings_mentioning(outcome, "negative control did not fire")


# --- evidence, controls and the runner -------------------------------------


@pytest.mark.parametrize(("gate_id", "assertion_id"), sorted(OWNED))
def test_every_check_emits_the_evidence_it_owns(ctx, gates, gate_id, assertion_id):
    outcome = run(ctx, gates, gate_id, assertion_id)
    expected = OWNED[(gate_id, assertion_id)]
    assert set(expected) <= set(outcome.evidence)
    assert set(expected) <= set(gates[gate_id].evidence_required)


@pytest.mark.parametrize(("gate_id", "assertion_id"), sorted(OWNED))
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gates, gate_id, assertion_id):
    control = run(ctx, gates, gate_id, assertion_id).evidence["negative_control_transcript"]
    assert control["probe"]
    assert control["why"]


@pytest.mark.parametrize(("gate_id", "assertion_id"), sorted(OWNED))
def test_every_check_binds_its_transcript_to_the_candidate_commit(ctx, gates, gate_id, assertion_id):
    outcome = run(ctx, gates, gate_id, assertion_id)
    binding = outcome.evidence.get("artifact_hashes_and_commit_binding")
    if binding is None:  # GATE-D1-09 does not name that artifact
        assert gate_id == "GATE-D1-09"
        return
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")


@pytest.mark.parametrize("gate_id", sorted(STILL_UNIMPLEMENTED))
def test_the_registered_gates_execute_these_assertions_through_the_runner(gate_id, monkeypatch):
    """End to end through ``GateRunner``, which is what merging the map buys.

    None of these four gates becomes fully covered by this module -- each still
    has assertions whose subject is not built -- so the honest expectation is
    ``PARTIALLY_EXECUTABLE`` and ``UNVERIFIABLE``, with these six assertions
    passing and the remainder still reported as ``NOT_IMPLEMENTED`` with a
    reason. A gate that reported PASS on the assertions that happen to be
    implemented is how a gate becomes decorative.
    """
    for key, check in CHECKS_AUDIT_FOLLOWUP.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([gate_id]).results[0]

    executed = {a.assertion_id for a in result.assertions if a.status is not AssertionStatus.NOT_IMPLEMENTED}
    not_implemented = {a.assertion_id for a in result.assertions if a.status is AssertionStatus.NOT_IMPLEMENTED}
    mine = {aid for (gid, aid) in CHECKS_AUDIT_FOLLOWUP if gid == gate_id}

    assert mine <= executed
    assert not_implemented == STILL_UNIMPLEMENTED[gate_id]
    assert result.failed == [], [(a.assertion_id, a.findings) for a in result.failed]
    #: Assertions deliberately staged to UNVERIFIABLE rather than PASS/FAIL, with
    #: the reason each one is staged. GATE-D2-10 A2 enumerates the composition
    #: debt -- 2 undeclared packages, 40 declared edges unproven by import, 5
    #: modules unreachable over real edges -- and reports it without flipping the
    #: gate red, so the owner sees the list before the colour change.
    #:
    #: **This carve-out is the thing to delete when A2 flips to FAIL.** Left
    #: undated and unexplained it becomes the next stale reason, which is the
    #: failure the D1-09 A3 test below exists to remember.
    staged = {("GATE-D2-10", "A2"): "enumerating composition debt pending owner decision"}
    staged_here = {aid for (gid, aid) in staged if gid == gate_id}
    assert all(
        a.status is AssertionStatus.PASS
        for a in result.assertions
        if a.assertion_id in mine - staged_here
    )
    assert all(
        a.status is AssertionStatus.UNVERIFIABLE
        for a in result.assertions
        if a.assertion_id in staged_here
    )
    assert result.executability is Executability.PARTIALLY_EXECUTABLE
    assert result.verdict is Verdict.UNVERIFIABLE
    assert all(a.note for a in result.assertions if a.assertion_id in not_implemented)


def test_the_runner_no_longer_reports_the_stale_reason_for_gate_d1_09_a3(monkeypatch):
    """The audit's one wrong entry, now corrected in both halves.

    ``NOT_EXECUTABLE_REASONS`` claimed "OTel span emission is not yet built",
    which was false: the emitters, the enforcement and the proving tests all
    existed. This test was written to assert the stale entry was STILL present,
    so that deleting it would trip here rather than pass unnoticed. It has now
    been deleted, so the assertion inverts -- the entry must be absent, and A3
    must execute rather than quote a reason.

    Kept rather than removed because the pair is the property worth holding: an
    assertion with a registered check must not also carry an excuse for not
    running, or the two can drift apart again without anything failing.
    """
    from evaluation.checks import NOT_EXECUTABLE_REASONS

    assert ("GATE-D1-09", "A3") not in NOT_EXECUTABLE_REASONS, (
        "the stale reason is back; an assertion cannot both have a check and an excuse"
    )
    monkeypatch.setitem(CHECKS, ("GATE-D1-09", "A3"), CHECKS_AUDIT_FOLLOWUP[("GATE-D1-09", "A3")])
    result = GateRunner().run(["GATE-D1-09"]).results[0]
    a3 = next(a for a in result.assertions if a.assertion_id == "A3")
    assert a3.status is AssertionStatus.PASS, a3.findings
    assert "not yet built" not in a3.note
