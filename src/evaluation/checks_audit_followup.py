"""The six assertions the 2026-08-02 gate audit left behind.

The audit that produced :mod:`evaluation.checks_d1_03` and
:mod:`evaluation.checks_d2_12` also turned up six assertions spread thinly
across five gates: one each in GATE-D1-09 and GATE-D3-23, two each in
GATE-D2-10 and GATE-D2-13. None is large enough to justify a module of its own
and none belongs to a gate whose other assertions are being written here, so
they share this file. What they have in common is not a subject -- it is a
finding: **in every one of these six cases the subject already existed and was
merely unchecked.**

One of them was worse than unchecked. ``checks.py`` records the reason

    ("GATE-D1-09", "A3"): "OTel span emission is not yet built (observability lane)."

and that is false. :mod:`observability.spans` has enforced the Section 23
correlation set since the observability lane landed; two live emitters run
through it; ``tests/unit/test_api_observability.py`` has been proving it for
just as long. The entry describes a plan, not the repository, and a stale
"not yet executable" is more dangerous than an absent one -- it stops anybody
looking again. :func:`d1_09_a3` executes the assertion. The
``NOT_EXECUTABLE_REASONS`` entry should be deleted; this module does not edit
``checks.py``, so the deletion is reported rather than performed.

Three judgement calls are load-bearing enough to state up front, because
getting any of them wrong produces a green that measured something adjacent to
the assertion:

* **"the eleven minimum correlation fields" is eleven supplied plus one
  derived.** :data:`observability.spans.REQUIRED_CORRELATION_FIELDS` has twelve
  entries; ``trace_id`` is the twelfth and is read off the live span context
  rather than accepted from a caller, because a caller-supplied trace id is a
  claim. A3 checks the eleven the assertion counts *and* the derived twelfth,
  and records the arity difference instead of resolving it by preference.
* **A prohibited component is deliberately present in the dependency graph.**
  The compiler emits every ``dependency-policy.yaml -> prohibited`` entry as a
  node so it can carry the ``conflicts_with`` edge that records *why* it is
  excluded. Asserting the component's absence would therefore fail on a
  correctly compiled graph, and "fix" it by deleting the owner's own exclusion
  record. :func:`d2_13_a4` asserts the property that actually matters: no
  prohibited node sits on an integration edge.
* **"No owner interrupt was raised" is proven by closure, not by counting.**
  Counting zero interrupts in a run that raised none proves nothing about the
  run that will. :func:`d3_23_a4` re-runs the AST scan that keeps
  ``langgraph.types.interrupt`` to one call site, then shows that call site
  refuses every reason ``autonomy-policy.yaml`` names in
  ``must_not_interrupt_for`` -- so the count is zero because it cannot be
  anything else.

Every check here carries a negative control that fails when the property is
false, and each control is itself checked for firing *for the right reason*: a
probe turned away by an unrelated guard records a green for a rule it never
reached.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from composition.registry import ModuleRegistry
from composition.root import build_registry
from contracts.compiler import CompiledProject, compile_pack
from drift.engine import ActiveTask, DriftEngine, DriftScanInput
from governance.envelope import CONTRACT_VERSION, content_hash
from governance.states import DriftFinding, OwnerInterrupt, ProjectState, TaskState, Verdict
from integrations.otel import OtelSettings, install_tracer_provider, reset_tracer_provider
from integrations.pack import load_pack
from observability.spans import (
    REQUIRED_BY_KIND,
    REQUIRED_CORRELATION_FIELDS,
    Correlation,
    IncompleteCorrelation,
    SpanKindName,
    efah_span,
    format_trace_id,
)
from ontology.schema import Blocker
from oracles import fixtures as fx
from oracles.oracle_001_composition import (
    CompositionSnapshot,
    EntryPoint,
    ModuleWiring,
    _reachable,
)
from owner_surface.domain import OpenBlocker
from workflows.interrupts import (
    PROHIBITED_INTERRUPT_REASONS,
    IllegalInterrupt,
    OwnerInterruptRequest,
    coerce_reason,
    owner_interrupt,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext
    from evaluation.gate_spec import AssertionSpec, GateSpec


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side
# fails then depends on which one Python happens to load first, so the same
# code works through the gate runner and explodes under pytest. The annotations
# above are strings (``from __future__ import annotations``), so they cost
# nothing at import time; ``ok`` and ``bad`` are the only runtime needs, and
# resolving them on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


def undecided(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import undecided as _undecided

    return _undecided(*args, **kwargs)


# ===========================================================================
# Shared subjects
# ===========================================================================


@functools.lru_cache(maxsize=4)
def _compiled(repo_root: Path) -> CompiledProject:
    """The compilation under test. Cached, and therefore never mutated."""
    return compile_pack(load_pack(repo_root / "project-pack"), repo_root=repo_root)


def _disposable_compilation(repo_root: Path) -> CompiledProject:
    """A fresh compilation for a negative control to vandalise.

    Deliberately uncached: the control below injects edges the compiler would
    never emit, and a mutation that leaked into the cached compilation would
    make a later check fail for a reason that has nothing to do with the
    product.
    """
    return compile_pack(load_pack(repo_root / "project-pack"), repo_root=repo_root)


def _load_pinned_module(path: Path, name: str) -> Any:
    """Load a pinned prover by path, the way ``checks.py`` loads its tools.

    The two scanners this file re-runs -- the interrupt call-site scan and the
    prohibited-import scan -- already exist and are already pinned by their
    suites. Re-implementing either here would let the gate check and the suite
    drift apart while both stayed green, which is exactly the failure a second
    implementation is supposed to catch.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _standard_evidence(
    ctx: GateContext, execution_log: dict[str, Any], negative_control: dict[str, Any]
) -> dict[str, Any]:
    """The three artifacts GATE-D2-10, GATE-D2-13 and GATE-D3-23 each name."""
    return {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }


# ===========================================================================
# GATE-D1-09 A3 — every emitted span carries the minimum correlation fields
# ===========================================================================

#: The eleven a caller supplies. ``trace_id`` is the twelfth entry of
#: ``REQUIRED_CORRELATION_FIELDS`` and is derived from the live span context, so
#: it is not in this tuple and must never be accepted as an input.
SUPPLIED_CORRELATION_FIELDS: tuple[str, ...] = tuple(
    field for field in REQUIRED_CORRELATION_FIELDS if field != "trace_id"
)
DERIVED_CORRELATION_FIELD = "trace_id"

#: The assertion's own count, kept as a number so a field added to
#: ``Correlation`` without a corresponding contract amendment fails A3 rather
#: than quietly becoming "the twelve minimum correlation fields".
ASSERTION_FIELD_COUNT = 11

#: Attribute names that open an OpenTelemetry span. ``observability.spans`` is
#: the only module permitted to call one; any other call site would be a span
#: that never passed through the correlation gate.
SPAN_OPENING_CALLS: frozenset[str] = frozenset({"start_as_current_span", "start_span"})
CORRELATION_GATE_MODULE = Path("observability") / "spans.py"


def _full_correlation() -> Correlation:
    """Every one of the eleven populated, so no emitter can be short by input.

    ``model_alias`` is a pack alias: :class:`Correlation` refuses a real vendor
    identity (Section 11.2), and a probe that tripped that guard would be
    testing the alias policy instead of the correlation set.
    """
    return Correlation(
        project_id="EFAH-001",
        task_id="TSK-D1-09-A3",
        work_unit_id="WU-D1-09-A3",
        run_id="RUN-D1-09-A3",
        model_alias="judge-j03",
        role="judge",
        terminus_commit="terminus-commit-d1-09-a3",
        repository_commit="repository-commit-d1-09-a3",
        evaluation_id="EVAL-D1-09-A3",
        oracle_version="1.0",
    )


def _span_openers(source: str, filename: str) -> list[str]:
    """Every span-opening call in one module, by attribute name."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    return sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in SPAN_OPENING_CALLS
        }
    )


def _live_emitter_spans(repo_root: Path, exporter: InMemorySpanExporter) -> list[Any]:
    """Drive two real emitters and return what they exported.

    ``POST /projects/import`` crosses
    :class:`api.middleware.correlation.CorrelationMiddleware` (an
    ``api_request`` span) and
    :meth:`api.controllers.projects.ProjectController.import_project` (a
    ``project`` span). Neither is written for this check and neither is mocked:
    the app, the container and the pack are the real ones. FastAPI is imported
    here rather than at module scope because ``checks.py`` imports this module,
    and paying for the web stack on every import of the registry is a cost the
    other five checks never asked for.
    """
    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.deps import Container
    from api.middleware.auth import TokenRegistry

    token = "owner-token-for-gate-d1-09-a3"
    app = create_app(
        container=Container.build(),
        token_registry=TokenRegistry(owner_token=token, service_token=None, worker_token=None),
        # The provider this check installed is already current; enabling
        # tracing here would install a second one and export the live spans
        # into a collector this check cannot read.
        enable_tracing=False,
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/projects/import",
        json={"pack_root": str(repo_root / "project-pack")},
        headers={"authorization": f"Bearer {token}"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"the live emitter probe did not reach the emitters: {response.text}")
    return list(exporter.get_finished_spans())


def d1_09_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``otel_span_field_assert`` -- expected ``all_of correlation_fields present``.

    Four arms, and the second is what makes the first mean anything.

    1. Every one of the eight emitter kinds opens a span with all eleven fields
       supplied, and each exported span is read back for all twelve attributes.
    2. For every emitter, and for every field that emitter must carry, the same
       span is opened with exactly that field emptied. Each must raise
       :class:`~observability.spans.IncompleteCorrelation` naming the field --
       and, checked separately because raising after exporting would still
       leave an uncorrelated span in the trace, nothing must reach the exporter.
    3. An AST scan proves ``observability.spans`` is the only module in ``src``
       that opens a span at all. Without it, arms 1 and 2 describe the spans
       that go through the correlation gate and say nothing about "every
       emitted span".
    4. Two live emitters are driven end to end, and what they emit is recorded
       field by field.

    Honest limit, recorded in the evidence rather than smoothed over: Section 23
    lists eleven fields as a *minimum correlation set*, and
    :data:`observability.spans.REQUIRED_BY_KIND` states which of them each
    emitter must carry -- a retrieval span has no ``evaluation_id`` to carry.
    So arm 1 proves every field is stamped when supplied, arm 2 proves each
    emitter's own minimum is enforced rather than defaulted, and the live spans
    in arm 4 are reported with the fields they carry *and* the fields they do
    not.
    """
    findings: list[str] = []

    declared = tuple(Correlation.__dataclass_fields__)
    field_set_report = {
        "assertion_claims": ASSERTION_FIELD_COUNT,
        "supplied_by_a_caller": list(SUPPLIED_CORRELATION_FIELDS),
        "derived_from_the_live_span_context": DERIVED_CORRELATION_FIELD,
        "module_constant_count": len(REQUIRED_CORRELATION_FIELDS),
        "correlation_dataclass_fields": list(declared),
        "why_the_counts_differ": (
            "the assertion counts the eleven a caller supplies; REQUIRED_CORRELATION_FIELDS "
            "carries twelve because trace_id is read off the live span context and is never "
            "accepted as an input -- a caller-supplied trace id is a claim, and Section 23 wants "
            "the trace to be evidence"
        ),
    }
    if len(SUPPLIED_CORRELATION_FIELDS) != ASSERTION_FIELD_COUNT:
        findings.append(
            f"the assertion names {ASSERTION_FIELD_COUNT} minimum correlation fields but the "
            f"module supplies {len(SUPPLIED_CORRELATION_FIELDS)}: {list(SUPPLIED_CORRELATION_FIELDS)}"
        )
    if set(SUPPLIED_CORRELATION_FIELDS) != set(declared):
        findings.append(
            f"Correlation declares {sorted(declared)}, which is not the Section 23 field list "
            f"{sorted(SUPPLIED_CORRELATION_FIELDS)}; a field outside the contract's set is either "
            "an uncounted correlation field or one the contract requires and nobody carries"
        )

    correlation = _full_correlation()
    emitted: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    live_error: str | None = None

    reset_tracer_provider()
    exporter = InMemorySpanExporter()
    try:
        install_tracer_provider(OtelSettings(synchronous_export=True), exporter=exporter)

        # --- arm 1: a fully correlated span from every emitter ------------
        for kind in SpanKindName:
            with efah_span(f"probe.{kind}", kind=kind, correlation=correlation) as span:
                context_trace_id = format_trace_id(span)
            exported = exporter.get_finished_spans()[-1]
            attributes = dict(exported.attributes or {})
            missing = [f for f in REQUIRED_CORRELATION_FIELDS if f"efah.{f}" not in attributes]
            stamped = attributes.get("efah.trace_id")
            emitted.append(
                {
                    "kind": str(kind),
                    "span_name": exported.name,
                    "declared_minimum_for_this_emitter": list(REQUIRED_BY_KIND[kind]),
                    "correlation_attributes_present": sorted(
                        key for key in attributes if key.startswith("efah.")
                    ),
                    "missing_correlation_fields": missing,
                    "trace_id_matches_the_span_context": stamped == context_trace_id,
                    "trace_id": stamped,
                }
            )
            if missing:
                findings.append(f"{kind}: a fully correlated span was emitted without {missing}")
            if stamped != context_trace_id:
                findings.append(
                    f"{kind}: efah.trace_id is {stamped!r} while the span context reads "
                    f"{context_trace_id!r}; the trace id is not derived from the span"
                )
            if not re.fullmatch(r"[0-9a-f]{32}", str(stamped or "")):
                findings.append(f"{kind}: efah.trace_id {stamped!r} is not a 32-hex trace id")

        # --- arm 2: drop one required field at a time ---------------------
        for kind in SpanKindName:
            for field_name in REQUIRED_BY_KIND[kind]:
                starved = replace(correlation, **{field_name: ""})
                before = len(exporter.get_finished_spans())
                record: dict[str, Any] = {
                    "kind": str(kind),
                    "field_emptied": field_name,
                    "refused": False,
                    "raised": None,
                    "named_the_field": False,
                    "spans_exported_by_the_refused_probe": 0,
                }
                try:
                    with efah_span("starved", kind=kind, correlation=starved):
                        pass
                except IncompleteCorrelation as exc:
                    record.update(
                        refused=True,
                        raised=type(exc).__name__,
                        named_the_field=field_name in exc.missing,
                        missing=list(exc.missing),
                    )
                except Exception as exc:  # pragma: no cover - a wrong-reason refusal
                    record.update(refused=True, raised=type(exc).__name__, detail=str(exc))
                record["spans_exported_by_the_refused_probe"] = (
                    len(exporter.get_finished_spans()) - before
                )
                refusals.append(record)
                if not record["refused"]:
                    findings.append(
                        f"{kind}: a span was opened with {field_name} empty; an uncorrelated span "
                        "cannot be joined to the run it came from"
                    )
                elif record["raised"] != IncompleteCorrelation.__name__:
                    findings.append(
                        f"{kind}: emptying {field_name} raised {record['raised']}, not "
                        "IncompleteCorrelation; the probe did not reach the correlation gate"
                    )
                elif not record["named_the_field"]:
                    findings.append(
                        f"{kind}: the refusal for {field_name} does not name it: "
                        f"{record.get('missing')}"
                    )
                if record["spans_exported_by_the_refused_probe"]:
                    findings.append(
                        f"{kind}: emptying {field_name} still exported "
                        f"{record['spans_exported_by_the_refused_probe']} span(s); refusing after "
                        "exporting leaves the uncorrelated span in the trace"
                    )

        # --- arm 4: the live emitters -------------------------------------
        exporter.clear()
        try:
            for span in _live_emitter_spans(ctx.repo_root, exporter):
                attributes = dict(span.attributes or {})
                kind_name = str(attributes.get("efah.span_kind", ""))
                kind = next((k for k in SpanKindName if str(k) == kind_name), None)
                required = list(REQUIRED_BY_KIND[kind]) if kind is not None else []
                absent = [f for f in required if f"efah.{f}" not in attributes]
                live.append(
                    {
                        "span_name": span.name,
                        "kind": kind_name,
                        "declared_minimum_for_this_emitter": required,
                        "minimum_fields_absent": absent,
                        "correlation_fields_carried": [
                            f for f in REQUIRED_CORRELATION_FIELDS if f"efah.{f}" in attributes
                        ],
                        "correlation_fields_not_carried": [
                            f for f in REQUIRED_CORRELATION_FIELDS if f"efah.{f}" not in attributes
                        ],
                        "trace_id": attributes.get("efah.trace_id"),
                        "trace_id_matches_the_span_context": (
                            attributes.get("efah.trace_id") == format(span.context.trace_id, "032x")
                        ),
                    }
                )
        except Exception as exc:
            live_error = f"{type(exc).__name__}: {exc}"
            findings.append(f"the live emitter probe did not run: {live_error}")
    finally:
        reset_tracer_provider()

    if live_error is None:
        if len(live) < 2:
            findings.append(
                f"the live emitter probe exported {len(live)} span(s); the API request and the "
                "project import are two distinct emitters and both must appear"
            )
        for record in live:
            if record["minimum_fields_absent"]:
                findings.append(
                    f"live span {record['span_name']!r} ({record['kind']}) is missing "
                    f"{record['minimum_fields_absent']} from its declared minimum"
                )
            if not record["trace_id"] or not record["trace_id_matches_the_span_context"]:
                findings.append(
                    f"live span {record['span_name']!r} carries trace_id {record['trace_id']!r}, "
                    "which is not its own span context's trace id"
                )
        traces = {record["trace_id"] for record in live}
        if len(live) >= 2 and len(traces) != 1:
            findings.append(
                f"the live spans carry {len(traces)} trace ids ({sorted(traces)}); a nested "
                "emitter that starts its own trace is not correlated to the request that caused it"
            )

    # --- arm 3: nothing else in src/ opens a span --------------------------
    src_root = ctx.repo_root / "src"
    gate_module = src_root / CORRELATION_GATE_MODULE
    offenders: list[str] = []
    scanned = 0
    for path in sorted(src_root.rglob("*.py")):
        if path == gate_module:
            continue
        scanned += 1
        opened = _span_openers(path.read_text(errors="ignore"), str(path))
        if opened:
            offenders.append(f"{path.relative_to(src_root).as_posix()}: {opened}")
    gate_opens_spans = _span_openers(gate_module.read_text(), str(gate_module))
    synthetic_opener = _span_openers(
        "def f(tracer):\n    with tracer.start_as_current_span('x'):\n        pass\n",
        "<negative-control>",
    )
    findings.extend(
        f"{offender} opens an OpenTelemetry span outside the Section 23 correlation gate"
        for offender in offenders
    )
    if not gate_opens_spans:
        findings.append(
            f"{CORRELATION_GATE_MODULE.as_posix()} no longer opens a span, so the scan above is "
            "looking for a call that has moved somewhere this check does not know about"
        )
    if not synthetic_opener:
        findings.append(
            "negative control did not fire: the span-opener scanner found nothing in a module "
            "that calls start_as_current_span"
        )

    otel_span_sample = {
        "check": a.method or "otel_span_field_assert",
        "expected": a.expected,
        "minimum_correlation_fields": field_set_report,
        "declared_minimum_per_emitter": {str(k): list(v) for k, v in REQUIRED_BY_KIND.items()},
        "fully_correlated_spans": emitted,
        "live_emitter_spans": live,
        "live_emitter_probe_error": live_error,
        "span_openers_outside_the_correlation_gate": {
            "files_scanned": scanned,
            "correlation_gate": CORRELATION_GATE_MODULE.as_posix(),
            "offenders": offenders,
            "calls_searched_for": sorted(SPAN_OPENING_CALLS),
        },
        "what_every_emitted_span_means_here": (
            "efah_span is the only span opener in src/, and it refuses to open a span missing the "
            "fields REQUIRED_BY_KIND states for that emitter. Section 23's eleven are a minimum "
            "correlation *set*, not a per-span obligation -- a retrieval span has no evaluation_id "
            "-- so the live spans are reported with the fields they carry and the fields they do "
            "not, rather than being scored against all eleven."
        ),
    }
    negative_control = {
        "probe": (
            "for every emitter and every field that emitter must carry, open the same span with "
            "exactly that field emptied; separately, scan src/ for a span opened outside the "
            "correlation gate"
        ),
        "why": (
            "a span carrying all twelve attributes proves the renderer works. It does not prove an "
            "uncorrelated span is refused, and it says nothing about spans opened by some other "
            "module that never passed through the gate. Both are how 'every emitted span carries "
            "the correlation fields' becomes false while this check stays green."
        ),
        "starved_span_probes": refusals,
        "probes_refused": sum(1 for r in refusals if r["refused"]),
        "probes_that_exported_a_span_anyway": sum(
            1 for r in refusals if r["spans_exported_by_the_refused_probe"]
        ),
        "span_opener_scanner_fires_on_a_synthetic_opener": synthetic_opener,
        "span_opener_scanner_still_sees_the_gate_itself": gate_opens_spans,
    }
    evidence = {
        "otel_span_sample": otel_span_sample,
        "negative_control_transcript": negative_control,
    }
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"all {len(SpanKindName)} emitters stamp the eleven supplied correlation fields and the "
            f"derived trace_id; {len(refusals)} probes emptying one required field each were "
            f"refused before export; {scanned} modules were scanned and none opens a span outside "
            f"the correlation gate; {len(live)} live spans carry their declared minimum on one trace"
        ),
    )


# ===========================================================================
# GATE-D2-10 A1 — a deliberately unregistered module fails the verifier
# ===========================================================================

#: The module the live probe unregisters. ``evaluation`` provides
#: ``gate_result``, which five other modules consume, so removing it exercises
#: both shapes of the Section 5.2 failure at once: an unresolved consumer and an
#: orphaned producer.
UNREGISTERED_PROBE_MODULE = "evaluation"

_ENTRYPOINTS = ("composition", "cli")


def _snapshot_from_registry(
    registry: ModuleRegistry, *, registered: list[str] | None = None
) -> CompositionSnapshot:
    """Render the live composition root as the subject ORACLE-001 decides on.

    ORACLE-001 never reads the filesystem -- that is what keeps its verdict path
    pure -- so somebody has to hand it the composition state. This is that
    translation, and it is deliberately mechanical: the modules, their nine
    wiring fields, and one edge per consumed capability pointing at whoever
    provides it. ``registered`` defaults to every declared module; passing a
    shorter list is how a module becomes "unit-tested but absent from the
    composition root" without anything else about it changing.
    """
    provided: dict[str, str] = {cap: "<composition-root>" for cap in registry.root_provides}
    for module, declaration in registry.declarations.items():
        for capability in declaration.provides:
            provided[capability] = module

    edges: list[tuple[str, str]] = []
    for module, declaration in registry.declarations.items():
        for capability in declaration.consumes:
            producer = provided.get(capability)
            if producer in registry.declarations and producer != module:
                edges.append((module, producer))

    modules = sorted(registry.declarations)
    return CompositionSnapshot(
        composition_root_parseable=True,
        declared_modules=modules,
        wiring={
            module: ModuleWiring(
                provides=list(declaration.provides),
                consumes=list(declaration.consumes),
                startup_registration=declaration.startup_registration,
                configuration_schema=declaration.configuration_schema or "",
                health_check=declaration.health_check or "",
                integration_test=declaration.integration_test or "",
                e2e_path=declaration.e2e_path or "",
                telemetry_span=declaration.telemetry_span or "",
                dashboard_projection=declaration.dashboard_projection or "",
            )
            for module, declaration in registry.declarations.items()
        },
        registered_modules=list(modules if registered is None else registered),
        entry_points=[
            EntryPoint(
                name="harness project run",
                approved_user_to_result_path=True,
                reaches=list(_ENTRYPOINTS),
            )
        ],
        invocation_edges=edges,
        import_edges=list(edges),
    )


def _decision_record(probe: str, decision: Any) -> dict[str, Any]:
    return {
        "probe": probe,
        "verdict": decision.verdict.value,
        "failure_state": decision.failure_state.value if decision.failure_state else None,
        "reasons": list(decision.reasons),
    }


def _finding_bodies(findings: Any) -> list[dict[str, Any]]:
    return [{"module": f.module, "kind": f.kind, "detail": f.detail} for f in findings]


def d2_10_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``negative_control_unregister_module`` -- expected ``composition_verifier_fails``.

    The pack already carries this exact case: ORACLE-001's KB-001 is "module
    with passing unit tests but absent from the composition root". Deciding it
    with the minted oracle is the first arm, and it is the one that ties the
    verdict to the owner's definition rather than to this file's reading of it.

    It is not enough on its own. A four-module fixture proves the oracle *can*
    fail; it does not prove the oracle would fail on this composition root,
    which carries every Section 5 module and a real capability graph. So the same
    probe runs twice more on live data: once through ORACLE-001 against a
    snapshot of the real registry with one module dropped from
    ``registered_modules``, and once through
    :meth:`composition.registry.ModuleRegistry.verify` against a copy of the
    real registry with that module's declaration deleted outright. Those are
    different failures -- declared but never constructed, versus not there at
    all -- and a verifier that catches one of them is half a verifier.

    The negative control is the unmutated registry: it must verify clean and
    the oracle must pass it. A composition verifier that rejects everything
    "fails an unregistered module" without ever having looked at one.
    """
    findings: list[str] = []
    oracle = ctx.oracles["ORACLE-001"]

    # --- arm 1: the pack's own fixture for this case ---------------------
    known_bad = next(f for f in fx.fixtures_for("ORACLE-001") if f.fixture_id == "KB-001")
    known_good = next(f for f in fx.fixtures_for("ORACLE-001") if f.fixture_id == "KG-001")
    fixture_decision = oracle.decide(known_bad.subject)
    fixture_control = oracle.decide(known_good.subject)

    if fixture_decision.verdict is not Verdict.FAIL:
        findings.append(
            f"KB-001 (unregistered module) produced {fixture_decision.verdict.value}, not FAIL"
        )
    if fixture_decision.failure_state is not TaskState.FAILED_WIRING:
        findings.append(
            f"KB-001 produced failure state {fixture_decision.failure_state}, not FAILED_WIRING"
        )
    if not any("absent from the composition root" in reason for reason in fixture_decision.reasons):
        findings.append(
            f"KB-001 failed for a reason other than registration: {fixture_decision.reasons}"
        )
    if fixture_control.verdict is not Verdict.PASS:
        findings.append(
            "negative control failed: the known-good composition was not accepted "
            f"({fixture_control.verdict.value}, {fixture_control.reasons})"
        )

    # --- arm 2: the live composition root, unmutated then mutated --------
    live_registry = build_registry()
    baseline_findings = live_registry.verify(entrypoints=set(_ENTRYPOINTS))
    live_snapshot = _snapshot_from_registry(live_registry)
    live_decision = oracle.decide(live_snapshot)

    if baseline_findings:
        findings.append(
            "negative control failed: the real composition root does not verify clean "
            f"({_finding_bodies(baseline_findings)}), so a failure on the mutated copy would "
            "prove nothing about the mutation"
        )
    if live_decision.verdict is not Verdict.PASS:
        findings.append(
            "negative control failed: ORACLE-001 does not accept the real composition root "
            f"({live_decision.verdict.value}, {live_decision.reasons})"
        )
    if UNREGISTERED_PROBE_MODULE not in live_registry.declarations:
        findings.append(
            f"the probe module {UNREGISTERED_PROBE_MODULE!r} is not declared at the composition "
            "root, so unregistering it proves nothing"
        )

    unregistered = [m for m in sorted(live_registry.declarations) if m != UNREGISTERED_PROBE_MODULE]
    unregistered_snapshot = _snapshot_from_registry(live_registry, registered=unregistered)
    unregistered_decision = oracle.decide(unregistered_snapshot)

    if unregistered_decision.verdict is not Verdict.FAIL:
        findings.append(
            f"unregistering {UNREGISTERED_PROBE_MODULE!r} in the live composition snapshot "
            f"produced {unregistered_decision.verdict.value}, not FAIL"
        )
    if unregistered_decision.failure_state is not TaskState.FAILED_WIRING:
        findings.append(
            f"the live unregistration produced {unregistered_decision.failure_state}, not "
            "FAILED_WIRING"
        )
    if not any(
        UNREGISTERED_PROBE_MODULE in reason and "absent from the composition root" in reason
        for reason in unregistered_decision.reasons
    ):
        findings.append(
            f"the live unregistration verdict does not name {UNREGISTERED_PROBE_MODULE!r} as "
            f"unregistered: {unregistered_decision.reasons}"
        )

    # --- arm 3: the registry verifier, on a copy with the module deleted --
    # ``build_registry`` constructs a fresh registry per call, so this mutation
    # cannot leak into arm 2's baseline or into anything else in the process.
    mutated_registry = build_registry()
    deleted = mutated_registry.declarations.pop(UNREGISTERED_PROBE_MODULE, None)
    orphaned_capabilities = list(deleted.provides) if deleted else []
    mutated_findings = mutated_registry.verify(entrypoints=set(_ENTRYPOINTS))
    unresolved = [
        f
        for f in mutated_findings
        if any(capability in f.detail for capability in orphaned_capabilities)
    ]

    if not mutated_findings:
        findings.append(
            f"deleting {UNREGISTERED_PROBE_MODULE!r} from the composition root produced no "
            "composition finding at all"
        )
    if not unresolved:
        findings.append(
            f"no finding names the capabilities {orphaned_capabilities} that "
            f"{UNREGISTERED_PROBE_MODULE!r} provided; the verifier noticed something else"
        )
    if any(f.kind != "MISSING_WIRING" for f in unresolved):
        findings.append(
            f"the unregistration was reported as {sorted({f.kind for f in unresolved})}, not "
            "MISSING_WIRING"
        )

    execution_log = {
        "check": a.method or "negative_control_unregister_module",
        "expected": a.expected,
        "pack_fixture": {
            "fixture_id": known_bad.fixture_id,
            "kind": known_bad.kind,
            "description": known_bad.description,
            "expected_verdict": known_bad.expected_verdict.value,
            **_decision_record(
                "ORACLE-001 decides the pack's unregistered-module fixture", fixture_decision
            ),
        },
        "live_composition_root": {
            "modules_declared": len(live_registry.declarations),
            "entrypoints": list(_ENTRYPOINTS),
            "invocation_edges": len(live_snapshot.invocation_edges),
            "baseline_composition_findings": _finding_bodies(baseline_findings),
            **_decision_record("ORACLE-001 decides the real composition root", live_decision),
        },
        "live_unregistration": {
            "module": UNREGISTERED_PROBE_MODULE,
            "how": "declared, wired and unit-tested, but dropped from registered_modules",
            **_decision_record(
                f"ORACLE-001 decides the real composition root with {UNREGISTERED_PROBE_MODULE!r} "
                "absent from the composition root",
                unregistered_decision,
            ),
        },
        "live_declaration_deleted": {
            "module": UNREGISTERED_PROBE_MODULE,
            "how": "the declaration removed from a fresh ModuleRegistry, as if never constructed",
            "capabilities_orphaned": orphaned_capabilities,
            "findings": _finding_bodies(mutated_findings),
        },
        "why_two_live_mutations": (
            "'unregistered' has two shapes. A module can be declared and wired but never "
            "constructed at the composition root (ORACLE-001's registration check), or it can be "
            "absent from the root entirely and leave its consumers unresolved "
            "(ModuleRegistry.verify). A verifier that catches one and not the other lets the "
            "other through."
        ),
    }
    negative_control = {
        "probe": "decide the unmutated composition root, and the pack's known-good fixture",
        "why": (
            "a composition verifier that fails everything satisfies 'an unregistered module fails' "
            "without having looked at registration. Both known-good arms must pass, or the "
            "mutated arms are measuring a verifier that was never selective."
        ),
        "known_good_fixture": _decision_record("KG-001", fixture_control),
        "real_composition_root": _decision_record("live registry, unmutated", live_decision),
        "real_registry_verify_findings": _finding_bodies(baseline_findings),
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            "an unregistered module fails the composition verifier on the pack's own fixture "
            "(KB-001, FAILED_WIRING) and twice on the live composition root -- ORACLE-001 names "
            f"{UNREGISTERED_PROBE_MODULE!r} absent from the root, and ModuleRegistry.verify reports "
            f"{len(mutated_findings)} MISSING_WIRING findings when its declaration is deleted -- "
            "while the unmutated root verifies clean"
        ),
    )


# ===========================================================================
# GATE-D2-10 A4 — architecture tests reject prohibited imports and cycles
# ===========================================================================

ARCHITECTURE_SUITE = "tests/architecture"
BOUNDARY_SCANNER = Path("tests") / "architecture" / "test_module_boundaries.py"

#: The pair the cycle control makes consume each other. ``oracles`` provides
#: ``oracle_verdict`` to ``evaluation``; giving ``oracles`` a taste for
#: ``gate_result`` closes the loop through the real capability graph rather than
#: through two invented modules.
_CYCLE_CONTROL = ("oracles", "gate_result")

_PYTEST_SUMMARY = re.compile(r"(\d+) passed")


def d2_10_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``architecture_test_run`` -- expected ``zero_violations``.

    The gate's method names a test run, so the pinned suite is executed as a
    subprocess and its exit status is the primary evidence -- the same
    delegation ``_d1_07`` and ``_d1_10`` use, and for the same reason: that
    suite, not this file, is what CI runs.

    A green exit status is a thin transcript, though. It records that nothing
    failed, not what was checked, and it is equally green for a suite that
    collected nothing. So the two properties the assertion actually names are
    also decided in process: :meth:`ModuleRegistry.cycles` over the live
    composition root, and the pinned import scanner from
    ``tests/architecture/test_module_boundaries.py`` re-run over ``src``. Both
    are then re-run with their defect injected, which is the part a subprocess
    exit status can never show: a cycle forced through the real capability
    graph, and a module importing two of the prohibited packages.
    """
    findings: list[str] = []

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", ARCHITECTURE_SUITE, "-q", "--no-header"],
        capture_output=True,
        text=True,
        cwd=ctx.repo_root,
        check=False,
    )
    output = (proc.stdout or proc.stderr).strip()
    tail = output.splitlines()
    summary = tail[-1] if tail else ""
    match = _PYTEST_SUMMARY.search(output)
    tests_passed = int(match.group(1)) if match else 0

    if proc.returncode != 0:
        findings.extend(tail[-8:] or [f"pytest exited {proc.returncode} with no output"])
    if tests_passed <= 0:
        findings.append(
            f"the architecture suite reported {tests_passed} passing tests; a suite that collected "
            "nothing has zero violations for the wrong reason"
        )

    # --- cycles, in process, on the live composition root -----------------
    registry = build_registry()
    cycle_findings = registry.cycles()
    cyclic = build_registry()
    cyclic.declarations[_CYCLE_CONTROL[0]].consumes.append(_CYCLE_CONTROL[1])
    injected_cycles = cyclic.cycles()

    findings.extend(
        f"circular dependency at the composition root: {f.module} -- {f.detail}"
        for f in cycle_findings
    )
    if not injected_cycles:
        findings.append(
            "negative control did not fire: making "
            f"{_CYCLE_CONTROL[0]!r} consume {_CYCLE_CONTROL[1]!r} produced no CIRCULAR_DEPENDENCY"
        )
    elif any(f.kind != "CIRCULAR_DEPENDENCY" for f in injected_cycles):
        findings.append(
            f"the injected cycle was reported as {sorted({f.kind for f in injected_cycles})}, not "
            "CIRCULAR_DEPENDENCY"
        )

    # --- prohibited imports, in process, with the pinned scanner ----------
    boundaries = _load_pinned_module(ctx.repo_root / BOUNDARY_SCANNER, "_gate_d2_10_boundaries")
    src_root = ctx.repo_root / "src"
    offenders: list[str] = []
    scanned = 0
    for path in sorted(src_root.rglob("*.py")):
        if path.parts[-2] == "adapters":
            continue
        scanned += 1
        prohibited = boundaries._module_imports(path) & boundaries.PROHIBITED
        if prohibited:
            offenders.append(f"{path.relative_to(src_root).as_posix()}: {sorted(prohibited)}")

    with tempfile.TemporaryDirectory() as directory:
        planted = Path(directory) / "prohibited_import_control.py"
        planted.write_text("import temporalio\nfrom anthropic import Client\n")
        control_hits = sorted(boundaries._module_imports(planted) & boundaries.PROHIBITED)

    findings.extend(f"prohibited import: {offender}" for offender in offenders)
    if not control_hits:
        findings.append(
            "negative control did not fire: the pinned import scanner found nothing in a module "
            "that imports two prohibited packages"
        )

    execution_log = {
        "check": a.method or "architecture_test_run",
        "expected": a.expected,
        "architecture_test_run": {
            "command": f"{Path(sys.executable).name} -m pytest {ARCHITECTURE_SUITE} -q --no-header",
            "cwd": str(ctx.repo_root),
            "returncode": proc.returncode,
            "tests_passed": tests_passed,
            "summary": summary,
        },
        "cycle_scan": {
            "subject": "composition.root.build_registry()",
            "modules": len(registry.declarations),
            "cycles": _finding_bodies(cycle_findings),
        },
        "prohibited_import_scan": {
            "scanner": BOUNDARY_SCANNER.as_posix(),
            "prohibited_packages": sorted(boundaries.PROHIBITED),
            "files_scanned": scanned,
            "offenders": offenders,
            "adapters_excluded": (
                "Section 5.1 keeps a vendor inside its adapter, so src/**/adapters/* is the one "
                "place a vendor import is legal; the pinned suite excludes it and so does this"
            ),
        },
        "why_the_suite_is_re_run_in_process": (
            "an exit status records that nothing failed. It does not record what was checked, and "
            "it is identical for a suite that collected no tests. The two properties the assertion "
            "names are therefore decided here as well, against the same pinned scanner the suite "
            "uses."
        ),
    }
    negative_control = {
        "probe": (
            f"make {_CYCLE_CONTROL[0]!r} consume {_CYCLE_CONTROL[1]!r} so the real capability graph "
            "closes a loop, and run the pinned import scanner over a module importing temporalio "
            "and anthropic"
        ),
        "why": (
            "zero violations is what a scanner that never fires reports. Each detector is shown "
            "firing on the exact defect the assertion forbids, on the same graph and with the same "
            "scanner used for the verdict."
        ),
        "injected_cycles": _finding_bodies(injected_cycles),
        "planted_prohibited_imports": control_hits,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"the pinned architecture suite passed {tests_passed} tests; the live composition root "
            f"carries no cycle across {len(registry.declarations)} modules; {scanned} source files "
            f"import none of {sorted(boundaries.PROHIBITED)}; and both detectors fire on an "
            "injected cycle and a planted prohibited import"
        ),
    )


# ===========================================================================
# GATE-D2-13 A2 — a custom duplicate of a selected dependency is rejected
# ===========================================================================


def _prohibited_components(ctx: GateContext) -> list[str]:
    """The owner's prohibited list, read from the pack rather than restated."""
    policy = ctx.pack_yaml("dependency-policy.yaml")
    return sorted(str(entry["component"]) for entry in policy.get("prohibited", []))


def _probe_task(project: CompiledProject, **overrides: Any) -> ActiveTask:
    """A real compiled task, reported back exactly as the plan describes it.

    Every field is copied from the compilation, so the only difference between
    the clean control and the injected probe is the component the probe claims
    to introduce. A task that drifted in some other way would be rejected for
    that instead, and the check would record a rejection it did not cause.
    """
    task_id = sorted(project.tasks)[0]
    task = project.tasks[task_id]
    params: dict[str, Any] = {
        "task_id": task_id,
        "title": task["title"],
        "requirement_ids": tuple(task["requirement_ids"]),
        "contract_version": CONTRACT_VERSION,
        "state": str(TaskState.RUNNING),
        "changed_paths": ("src/contracts/compiler.py",),
        "allowed_paths": tuple(task["allowed_paths"]),
        "prohibited_paths": tuple(task["prohibited_paths"]),
    }
    params.update(overrides)
    return ActiveTask(**params)


def d2_13_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A2 ``negative_control_inject_custom_duplicate`` -- ``rejected_with UNSUPPORTED_REIMPLEMENTATION``.

    The assertion's method is itself a negative control, which makes the control
    this check owes it the *inverse* arm: the detector must fire on an injected
    duplicate and must not fire on anything else. Four arms hold it to that.

    * The injection: one active task, identical to a real compiled task in every
      field, claiming to introduce a component the owner's
      ``dependency-policy.yaml`` names prohibited. Run once per prohibited
      component, because a detector that catches ``custom_workflow_engine`` and
      misses ``custom_vector_index`` is a list somebody edited by hand.
    * The clean control: the same task introducing nothing, which must draw no
      finding at all.
    * The two discriminating controls: the same injection carrying a
      BUILD_VS_INTEGRATE record, and the same injection naming a component that
      duplicates nothing. Section 14.2 forbids *unrecorded* reimplementation; an
      engine that rejected the recorded decision too would be blocking the
      contract's own escape hatch, and one that rejected any new component would
      be blocking work rather than drift.

    Honest limit: the engine's condition is that a record is *present*. Whether
    that record carries all seven Section 14.2 fields is A3's assertion, not
    this one, and this check does not claim it.
    """
    findings: list[str] = []
    project = _compiled(ctx.repo_root)
    engine = DriftEngine(project)
    required_finding = str(DriftFinding.UNSUPPORTED_REIMPLEMENTATION)

    declared_prohibited = _prohibited_components(ctx)
    engine_prohibited = sorted(engine.prohibited_components)
    if engine_prohibited != declared_prohibited:
        findings.append(
            f"the drift engine guards {engine_prohibited} while dependency-policy.yaml prohibits "
            f"{declared_prohibited}; the injection below is being scored against the wrong list"
        )
    if a.failure_state != required_finding:
        findings.append(
            f"the gate declares failure_state {a.failure_state!r} while the engine emits "
            f"{required_finding!r}; a rejection under either name would not satisfy the other"
        )

    def scan(task: ActiveTask) -> Any:
        return engine.scan(DriftScanInput(compiled=project, active_tasks=[task]))

    clean_report = scan(_probe_task(project))

    injections: list[dict[str, Any]] = []
    for component in declared_prohibited:
        report = scan(_probe_task(project, introduces_components=(component,)))
        hits = report.of_type(DriftFinding.UNSUPPORTED_REIMPLEMENTATION)
        injections.append(
            {
                "component": component,
                "findings": [f.as_body() for f in report.findings],
                "unsupported_reimplementation_count": len(hits),
                "blocks": all(f.blocks for f in hits),
                "names_the_component": all(component in f.detail for f in hits),
                "terminal_state": report.terminal_state.value,
                "unresolved_scope_drift": report.unresolved_scope_drift,
            }
        )
        if len(hits) != 1:
            findings.append(
                f"introducing {component!r} with no BUILD_VS_INTEGRATE record produced "
                f"{len(hits)} {required_finding} findings, not 1: {report.types_found()}"
            )
            continue
        if not all(f.blocks for f in hits):
            findings.append(f"the {required_finding} for {component!r} does not block")
        if not all(component in f.detail for f in hits):
            findings.append(
                f"the {required_finding} for {component!r} does not name it: {hits[0].detail}"
            )
        if report.terminal_state is not ProjectState.FAILED_CONTRACT:
            findings.append(
                f"introducing {component!r} left the project in {report.terminal_state.value}"
            )

    recorded_component = declared_prohibited[0] if declared_prohibited else ""
    recorded_report = scan(
        _probe_task(
            project,
            introduces_components=(recorded_component,),
            build_vs_integrate_record={
                "component": recorded_component,
                "decision": "build",
                "recorded_blocker": "negative control: an evidence-backed Section 14.2 decision",
            },
        )
    )
    novel_report = scan(
        _probe_task(project, introduces_components=("efah_component_that_duplicates_nothing",))
    )

    if clean_report.findings:
        findings.append(
            "negative control failed: a task copied verbatim from the compiled plan already draws "
            f"{clean_report.types_found()}, so the injected task's rejection is not the injection's "
            "doing"
        )
    if recorded_report.of_type(DriftFinding.UNSUPPORTED_REIMPLEMENTATION):
        findings.append(
            "negative control failed: an introduction carrying a BUILD_VS_INTEGRATE record was "
            f"still rejected as {required_finding}; Section 14.2 forbids unrecorded "
            "reimplementation, not recorded decisions"
        )
    if novel_report.of_type(DriftFinding.UNSUPPORTED_REIMPLEMENTATION):
        findings.append(
            "negative control failed: introducing a component that duplicates nothing was rejected "
            f"as {required_finding}; the detector is firing on introduction, not on duplication"
        )

    execution_log = {
        "check": a.method or "negative_control_inject_custom_duplicate",
        "expected": a.expected,
        "declared_failure_state": a.failure_state,
        "prohibited_components": declared_prohibited,
        "prohibited_components_the_engine_derived": engine_prohibited,
        "probe_task": sorted(project.tasks)[0],
        "injections": injections,
        "what_the_engine_checks": (
            "DriftEngine.scan raises UNSUPPORTED_REIMPLEMENTATION when an active task introduces a "
            "component the compiled graph marks prohibited and carries no BUILD_VS_INTEGRATE "
            "record. Whether that record holds all seven Section 14.2 fields is A3's assertion; "
            "this check does not claim it."
        ),
    }
    negative_control = {
        "probe": (
            "the same compiled task introducing nothing; introducing a prohibited component with a "
            "BUILD_VS_INTEGRATE record; and introducing a component that duplicates nothing"
        ),
        "why": (
            "the assertion's own method is a negative control, so what it needs from this check is "
            "the inverse: an engine that flagged every task, or every introduction, would reject "
            "the injected duplicate too and would have proven nothing about duplication."
        ),
        "clean_task": {
            "findings": [f.as_body() for f in clean_report.findings],
            "terminal_state": clean_report.terminal_state.value,
        },
        "introduction_with_a_recorded_decision": {
            "component": recorded_component,
            "findings": [f.as_body() for f in recorded_report.findings],
        },
        "introduction_that_duplicates_nothing": {
            "component": "efah_component_that_duplicates_nothing",
            "findings": [f.as_body() for f in novel_report.findings],
        },
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"each of the {len(declared_prohibited)} components dependency-policy.yaml prohibits is "
            f"rejected as {required_finding} when introduced without a BUILD_VS_INTEGRATE record, "
            "while the same task introducing nothing, introducing it with a recorded decision, and "
            "introducing a component that duplicates nothing all pass"
        ),
    )


# ===========================================================================
# GATE-D2-13 A4 — no prohibited component is on an integration edge
# ===========================================================================

#: The one edge a prohibited component may carry, and only outbound. The
#: compiler emits ``PKG:custom_vector_index --conflicts_with--> PKG:lancedb`` to
#: record *why* the component is excluded; that edge is the exclusion, not an
#: integration.
DECLARED_EXCLUSION_EDGE = "conflicts_with"


def _prohibited_nodes(project: CompiledProject) -> dict[str, str]:
    """Prohibited ``SoftwarePackage`` nodes, mapped back to component names."""
    return {
        node_id: node_id.removeprefix("PKG:")
        for node_id, node in project.graph.nodes.items()
        if node.kind == "SoftwarePackage" and node.attributes.get("prohibited")
    }


def _integration_edges(project: CompiledProject, node_id: str) -> list[dict[str, Any]]:
    """Every edge incident on ``node_id`` that is not its own exclusion record."""
    return [
        edge.as_body()
        for edge in project.graph.edges
        if node_id in (edge.source, edge.target)
        and not (edge.edge_type == DECLARED_EXCLUSION_EDGE and edge.source == node_id)
    ]


def d2_13_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``prohibited_component_scan`` -- expected ``zero_matches``.

    The literal scan the method name suggests -- search the dependency graph for
    a prohibited component and require no match -- fails on a correctly compiled
    graph, and the obvious way to "fix" it is to stop emitting the owner's
    exclusion records. ``_compile_dependencies`` adds every
    ``dependency-policy.yaml -> prohibited`` entry as a node precisely so it can
    carry the ``conflicts_with`` edge naming the selected dependency it
    duplicates. Deleting those nodes would delete the evidence that the
    exclusion was ever made.

    So the match this check looks for is an *integration*: any edge incident on
    a prohibited node other than that node's own outbound ``conflicts_with``. A
    task that depends on it, a package it is compatible with, an artifact it
    produces -- each would mean the component is in the graph as something the
    build uses rather than as something the owner excluded.

    Two things keep the scan from being vacuous. The prohibited node set is
    reconciled against the pack's own list, because a scan over an empty set
    returns zero matches; and the compiler's emitted ``efah.dependency`` objects
    are scanned as a second, independent view, because the in-memory graph is
    the compiler agreeing with itself.
    """
    findings: list[str] = []
    project = _compiled(ctx.repo_root)
    graph = project.graph

    declared = _prohibited_components(ctx)
    nodes = _prohibited_nodes(project)
    node_components = sorted(nodes.values())

    if node_components != declared:
        findings.append(
            f"dependency-policy.yaml prohibits {declared} but the compiled graph marks "
            f"{node_components} prohibited; a component missing from the graph is a component this "
            "scan never looked at"
        )
    if not nodes:
        findings.append(
            "the compiled graph carries no prohibited SoftwarePackage node, so 'zero matches' is a "
            "statement about an empty search"
        )

    selected = ctx.pack_yaml("dependency-policy.yaml").get("selected_stack", {})
    selected_components = sorted(str(spec["component"]) for spec in selected.values())
    overlap = sorted(set(selected_components) & set(declared))
    if overlap:
        findings.append(f"components both selected and prohibited by the policy: {overlap}")

    per_node: list[dict[str, Any]] = []
    for node_id, component in sorted(nodes.items()):
        integration = _integration_edges(project, node_id)
        exclusions = [
            edge.as_body()
            for edge in graph.edges
            if edge.source == node_id and edge.edge_type == DECLARED_EXCLUSION_EDGE
        ]
        per_node.append(
            {
                "node": node_id,
                "component": component,
                "declared_exclusion_edges": exclusions,
                "integration_edges": integration,
            }
        )
        findings.extend(
            f"prohibited component {component!r} is on an integration edge: "
            f"{edge['source']} --{edge['edge_type']}--> {edge['target']}"
            for edge in integration
        )

    # Second view: what the compiler emitted, not what it is holding.
    emitted_edges = [
        obj.body
        for obj in project.outputs.get("dependencies_and_critical_path", [])
        if obj.envelope.schema_id == "efah.dependency"
    ]
    emitted_matches = [
        body
        for body in emitted_edges
        if (set(nodes) & {body.get("source"), body.get("target")})
        and not (body.get("edge_type") == DECLARED_EXCLUSION_EDGE and body.get("source") in nodes)
    ]
    findings.extend(
        "an emitted efah.dependency puts a prohibited component on an integration edge: "
        f"{body['source']} --{body['edge_type']}--> {body['target']}"
        for body in emitted_matches
    )
    if len(emitted_edges) != len(graph.edges):
        findings.append(
            f"the compiler emitted {len(emitted_edges)} efah.dependency objects for "
            f"{len(graph.edges)} graph edges; the two views of the dependency graph disagree"
        )

    # --- negative control: put a prohibited component on two real edges ---
    control = _disposable_compilation(ctx.repo_root)
    control_nodes = _prohibited_nodes(control)
    injected: list[str] = []
    if control_nodes and control.graph.nodes_of_kind("Task"):
        task_node = sorted(control.graph.nodes_of_kind("Task"))[0]
        victim = sorted(control_nodes)[0]
        control.graph.add_edge(
            task_node,
            victim,
            "depends_on",
            dependency_class="software_package",
            rationale="negative control: a task integrating a prohibited component",
        )
        injected.append(f"depends_on {task_node} -> {victim}")
        if "PKG:python" in control.graph.nodes:
            second = sorted(control_nodes)[-1]
            control.graph.add_edge(
                second,
                "PKG:python",
                "compatible_with",
                dependency_class="software_package",
                rationale="negative control: a prohibited component entering the selected stack",
            )
            injected.append(f"compatible_with {second} -> PKG:python")

    control_hits = {
        node_id: _integration_edges(control, node_id)
        for node_id in sorted(control_nodes)
        if _integration_edges(control, node_id)
    }
    if not injected:
        findings.append(
            "the negative control could not be built: the disposable compilation carries no "
            "prohibited node or no Task node to attach one to"
        )
    elif len(control_hits) != len(injected):
        findings.append(
            f"negative control did not fire: {len(injected)} injected integration edges produced "
            f"{len(control_hits)} matches ({sorted(control_hits)})"
        )

    execution_log = {
        "check": a.method or "prohibited_component_scan",
        "expected": a.expected,
        "prohibited_by_the_owner_policy": declared,
        "prohibited_nodes_in_the_compiled_graph": sorted(nodes),
        "selected_stack_components": selected_components,
        "per_prohibited_component": per_node,
        "graph_edge_count": len(graph.edges),
        "emitted_dependency_objects": len(emitted_edges),
        "emitted_integration_matches": emitted_matches,
        "what_counts_as_a_match": (
            "any edge incident on a prohibited node other than that node's own outbound "
            f"{DECLARED_EXCLUSION_EDGE!r}. The prohibited nodes are present on purpose: the "
            "compiler emits them so the owner's exclusion is recorded as a typed edge, so "
            "asserting their absence would fail on a correct graph and reward deleting the "
            "exclusion record."
        ),
    }
    negative_control = {
        "probe": (
            "in a disposable compilation of the same pack, make a real task depend on a prohibited "
            "component and make another prohibited component compatible with the language runtime"
        ),
        "why": (
            "'zero matches' is what a scan over an empty node set reports, and what a scan that "
            "treats every conflicts_with edge as permission reports. Injecting the two shapes an "
            "integration would take proves the scan can still see one."
        ),
        "injected": injected,
        "matches_after_injection": control_hits,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"all {len(nodes)} prohibited components appear in the graph only as the owner's own "
            f"exclusion records -- no integration edge touches any of them across "
            f"{len(graph.edges)} edges and {len(emitted_edges)} emitted efah.dependency objects -- "
            "and injected integration edges are caught"
        ),
    )


# ===========================================================================
# GATE-D3-23 A4 — no owner interrupt for test, integration or CI repair
# ===========================================================================

INTERRUPT_SCANNER = Path("tests") / "unit" / "test_workflow_interrupts.py"
INTERRUPT_GATE_MODULE = Path("workflows") / "interrupts.py"

#: The reasons this assertion is specifically about, from ``must_not_interrupt_for``.
_REPAIR_REASONS = ("ordinary_test_failure", "integration_failure", "ci_failure_repair")


def _blocker_record_refuses(cls: Any, prohibited: str, legal: str, **fields: Any) -> dict[str, Any]:
    """Does this record type refuse a prohibited interrupt reason, by name?

    Constructing either blocker type also raises for its missing required
    fields, so "it raised" is not evidence. The error locations are read
    instead: ``interrupt_type`` must appear among them for the prohibited value
    and must not appear for a permitted one.
    """

    def locations(value: str) -> list[str]:
        try:
            cls(interrupt_type=value, **fields)
        except ValidationError as exc:
            return sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        return []

    prohibited_locations = locations(prohibited)
    legal_locations = locations(legal)
    return {
        "record_type": f"{cls.__module__}.{cls.__name__}",
        "declared_type": str(cls.model_fields["interrupt_type"].annotation),
        "prohibited_value": prohibited,
        "rejected_on_interrupt_type": "interrupt_type" in prohibited_locations,
        "prohibited_value_error_fields": prohibited_locations,
        "permitted_value": legal,
        "permitted_value_rejected_on_interrupt_type": "interrupt_type" in legal_locations,
    }


def d3_23_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``interrupt_type_audit`` -- ``zero_interrupts_outside_human_interrupts_only``.

    Counting the interrupts a run happened to raise proves nothing about the
    next run, so this is decided by closure instead, in four steps.

    1. The type set is closed at :class:`~governance.states.OwnerInterrupt`, and
       it must equal ``autonomy-policy.yaml -> human_interrupts_only`` exactly.
       :data:`~workflows.interrupts.PROHIBITED_INTERRUPT_REASONS` must equal
       ``must_not_interrupt_for`` exactly. A check written against a list the
       owner did not write is a check of this file's memory.
    2. The call sites are closed to one. The AST scan that
       ``tests/unit/test_workflow_interrupts.py`` pins is re-run over ``src`` --
       the pinned function itself, not a copy of it.
    3. That one call site refuses every prohibited reason.
       :func:`~workflows.interrupts.coerce_reason` is exercised over all nine,
       and :func:`~workflows.interrupts.owner_interrupt` is called with a
       request whose reason was forged past pydantic, to show the gate is at the
       call site and not only in the constructor.
    4. The recorded blockers are audited: the compiler's emitted
       ``efah.escalation_condition`` objects, and the two record types a blocker
       is stored as.

    Honest limit, recorded rather than implied: there is no interrupt *log* to
    read. Raised interrupts live in LangGraph checkpoints and answered blockers
    live behind the owner-surface gateway, neither of which a gate check reaches
    offline. What is proven is that no reason outside the seven can be raised or
    recorded at all -- which is why the count is zero.
    """
    findings: list[str] = []
    policy = ctx.pack_yaml("autonomy-policy.yaml")

    # --- 1. the two lists, against the owner's own file --------------------
    declared_types = sorted(str(i) for i in OwnerInterrupt)
    pack_types = sorted(str(x) for x in policy.get("human_interrupts_only", []))
    pack_prohibited = sorted(str(x) for x in policy.get("must_not_interrupt_for", []))
    if declared_types != pack_types:
        findings.append(
            f"OwnerInterrupt is {declared_types} while autonomy-policy.yaml permits {pack_types}"
        )
    if sorted(PROHIBITED_INTERRUPT_REASONS) != pack_prohibited:
        findings.append(
            f"PROHIBITED_INTERRUPT_REASONS is {sorted(PROHIBITED_INTERRUPT_REASONS)} while "
            f"autonomy-policy.yaml forbids interrupting for {pack_prohibited}"
        )
    missing_repair_reasons = [r for r in _REPAIR_REASONS if r not in pack_prohibited]
    if missing_repair_reasons:
        findings.append(
            f"the assertion is about test, integration and CI repair, and {missing_repair_reasons} "
            "are not in must_not_interrupt_for; the audit below would not cover them"
        )

    # --- 2. one call site, proven by the pinned scanner --------------------
    scanner = _load_pinned_module(ctx.repo_root / INTERRUPT_SCANNER, "_gate_d3_23_interrupt_scan")
    src_root = ctx.repo_root / "src"
    gate_module = src_root / INTERRUPT_GATE_MODULE
    sources = sorted(src_root.rglob("*.py"))
    offenders = [
        path.relative_to(src_root).as_posix()
        for path in sources
        if path != gate_module and scanner._calls_langgraph_interrupt(path)
    ]
    scanner_sees_the_gate = scanner._calls_langgraph_interrupt(gate_module)
    findings.extend(
        f"{offender} can raise a LangGraph interrupt outside the Section 10.7 gate"
        for offender in offenders
    )
    if not scanner_sees_the_gate:
        findings.append(
            "negative control did not fire: the pinned scanner no longer detects the interrupt "
            f"call in {INTERRUPT_GATE_MODULE.as_posix()}, so the scan above proves nothing"
        )

    # --- 3. the call site refuses every prohibited reason ------------------
    refusals: list[dict[str, Any]] = []
    for reason in sorted(PROHIBITED_INTERRUPT_REASONS):
        record: dict[str, Any] = {"reason": reason, "raised": None, "diagnosed": False}
        try:
            coerce_reason(reason)
        except IllegalInterrupt as exc:
            record.update(raised=type(exc).__name__, diagnosed="must_not_interrupt_for" in str(exc))
        except Exception as exc:  # pragma: no cover - a wrong-reason refusal
            record.update(raised=type(exc).__name__, detail=str(exc))
        refusals.append(record)
        if record["raised"] != IllegalInterrupt.__name__:
            findings.append(
                f"coerce_reason({reason!r}) raised {record['raised']}; a reason "
                "autonomy-policy.yaml forbids must be refused as an IllegalInterrupt"
            )
        elif not record["diagnosed"]:
            findings.append(
                f"the refusal of {reason!r} does not name must_not_interrupt_for, so the caller is "
                "told no without being told to continue autonomously"
            )

    accepted: list[str] = []
    for reason in OwnerInterrupt:
        try:
            accepted.append(str(coerce_reason(str(reason))))
        except IllegalInterrupt:
            findings.append(
                f"negative control failed: the permitted reason {reason} was refused; a gate that "
                "refuses everything satisfies 'zero prohibited interrupts' by refusing the owner too"
            )

    forged = OwnerInterruptRequest.model_construct(
        reason="ci_failure_repair",
        what_it_blocks="a red CI run",
        options=["repair it", "ask the owner"],
        consequence_of_each_option=["continue autonomously", "stall the build"],
    )
    try:
        owner_interrupt(forged)
        forged_result: dict[str, Any] = {"raised": None, "reached_langgraph": True}
        findings.append(
            "owner_interrupt() accepted a request whose reason was forged past pydantic; the "
            "Section 10.7 gate is in the constructor only, so a hand-built request reaches LangGraph"
        )
    except IllegalInterrupt as exc:
        forged_result = {
            "raised": type(exc).__name__,
            "reached_langgraph": False,
            "detail": str(exc),
        }
    except Exception as exc:
        forged_result = {
            "raised": type(exc).__name__,
            "reached_langgraph": False,
            "detail": str(exc),
        }
        findings.append(
            f"owner_interrupt() refused the forged request with {type(exc).__name__} rather than "
            "IllegalInterrupt; the probe did not reach the Section 10.7 gate"
        )

    # --- 4. the recorded blockers -----------------------------------------
    project = _compiled(ctx.repo_root)
    conditions = [
        obj.body
        for obj in project.outputs.get("human_escalation_conditions", [])
        if obj.envelope.schema_id == "efah.escalation_condition"
    ]
    recorded_types = sorted({str(body.get("interrupt_type")) for body in conditions})
    prohibited_recorded = [t for t in recorded_types if t in PROHIBITED_INTERRUPT_REASONS]
    outside_the_seven = [t for t in recorded_types if t not in declared_types]
    if not conditions:
        findings.append(
            "the compilation recorded no escalation condition, so the interrupt-type audit has "
            "nothing to audit"
        )
    if prohibited_recorded:
        findings.append(
            "a recorded escalation condition names a must_not_interrupt_for reason: "
            f"{prohibited_recorded}"
        )
    if outside_the_seven:
        findings.append(
            "a recorded escalation condition names an interrupt type outside Section 10.7: "
            f"{outside_the_seven}"
        )

    record_types = [
        _blocker_record_refuses(
            OpenBlocker,
            "ci_failure_repair",
            str(OwnerInterrupt.OWNER_SCOPE_DECISION),
            blocker_id="B-D3-23-A4",
            question="does this record type accept a CI repair as an owner blocker?",
        ),
        _blocker_record_refuses(
            Blocker,
            "ci_failure_repair",
            str(OwnerInterrupt.OWNER_SCOPE_DECISION),
            description="does the authoritative record type accept a CI repair?",
        ),
    ]
    for record in record_types:
        if not record["rejected_on_interrupt_type"]:
            findings.append(
                f"{record['record_type']} accepts interrupt_type={record['prohibited_value']!r}; a "
                "blocker for CI repair could be recorded in the control plane"
            )
        if record["permitted_value_rejected_on_interrupt_type"]:
            findings.append(
                f"negative control failed: {record['record_type']} also rejects the permitted value "
                f"{record['permitted_value']!r}"
            )

    execution_log = {
        "check": a.method or "interrupt_type_audit",
        "expected": a.expected,
        "closed_interrupt_type_set": declared_types,
        "autonomy_policy_human_interrupts_only": pack_types,
        "autonomy_policy_must_not_interrupt_for": pack_prohibited,
        "reasons_this_assertion_names": list(_REPAIR_REASONS),
        "call_site_scan": {
            "scanner": INTERRUPT_SCANNER.as_posix(),
            "gate_module": INTERRUPT_GATE_MODULE.as_posix(),
            "files_scanned": len(sources),
            "offenders": offenders,
        },
        "prohibited_reason_refusals": refusals,
        "permitted_reasons_accepted": accepted,
        "recorded_escalation_conditions": conditions,
        "recorded_interrupt_types": recorded_types,
        "recorded_blocker_types": record_types,
        "what_is_not_measured_here": (
            "there is no interrupt log to read. A raised interrupt lives in a LangGraph checkpoint "
            "and an answered blocker lives behind the owner-surface gateway, neither of which a "
            "gate check reaches offline. What is proven is that no reason outside the seven can be "
            "raised or recorded at all, which is what makes the count zero."
        ),
    }
    negative_control = {
        "probe": (
            "call the only interrupt call site with a reason forged past pydantic; construct both "
            "blocker record types with a must_not_interrupt_for reason; and run the pinned scanner "
            "against the gate module itself"
        ),
        "why": (
            "'no interrupt was raised for CI repair' is true of a system that raises no interrupts "
            "at all, and of a scanner that detects none. So the permitted seven must still be "
            "accepted, the record types must still accept them, and the scanner must still see the "
            "one call site it is meant to find."
        ),
        "forged_reason_at_the_only_call_site": forged_result,
        "record_types": record_types,
        "scanner_still_detects_the_gate_module": scanner_sees_the_gate,
        "permitted_reasons_still_accepted": accepted,
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"the interrupt type set is closed at the contract's {len(declared_types)} and matches "
            f"autonomy-policy.yaml; one call site exists in src/; all {len(refusals)} "
            "must_not_interrupt_for reasons are refused there, including one forged past pydantic; "
            f"and all {len(conditions)} recorded escalation conditions name a permitted type"
        ),
    )


# ===========================================================================
# GATE-D2-10 A2 — reachability from an approved user-to-result path
# ===========================================================================


def _capability_edges(registry: ModuleRegistry) -> set[tuple[str, str]]:
    """``(consumer, producer)`` for every capability a declared module consumes.

    This is the same join ``_snapshot_from_registry`` performs; it is lifted out
    so A2 can compare the *claim* against the import graph rather than against
    another copy of itself.
    """
    provided = {cap: mod for mod, dec in registry.declarations.items() for cap in dec.provides}
    edges: set[tuple[str, str]] = set()
    for module, declaration in registry.declarations.items():
        for capability in declaration.consumes:
            producer = provided.get(capability)
            if producer in registry.declarations and producer != module:
                edges.add((module, producer))
    return edges


def d2_10_a2(ctx: GateContext, gate: GateSpec, assertion: AssertionSpec) -> AssertionOutcome:
    """A2 ``reachability_analysis_from_composition_root`` -> ``zero_unreachable_modules``.

    **Staged deliberately: this returns UNVERIFIABLE, never FAIL, until the owner
    has read the debt it enumerates.** Flipping it is a one-line change at the
    end of this function, and the reason it has not been flipped is written here
    rather than left as silence -- which is the failure this gate exists to catch.

    A1 proves the oracle *can* fail, by mutating a snapshot. It cannot prove the
    composition root is wired, because the edges it hands the oracle are the
    ``consumes`` strings from ``build_registry`` and
    ``_snapshot_from_registry:642`` passes that same list as ``import_edges``.
    The independent second checker is a copy of the first. So a module is
    "reachable" as soon as somebody types a capability name.

    A2 asks the question against the code: every first-party package under
    ``src/`` must be declared, every declared capability edge must be backed by
    an import somewhere, and every package must be reachable from an approved
    entry point over *real* edges.

    One honest limit, stated because it changes how the findings read: an edge
    can be genuine without an import. ``workflows`` consumes ``worker_session``
    and never imports ``workers`` -- the session arrives through
    ``WorkflowServices`` as a Protocol, constructed at the composition root.
    Dependency injection is not a fabricated edge. So unbacked edges are
    reported as **unproven**, not as false, and closing them means either an
    import or a composition-root construction the harness can point at.
    """
    from composition import inventory

    packages = inventory.first_party_packages()
    sites = inventory.edge_sites()
    real = set(sites)
    registry = build_registry()
    declared = _capability_edges(registry)

    findings: list[str] = []

    undeclared = sorted(set(packages) - set(registry.declarations))
    findings.extend(
        f"src/{module}/ exists under src/ but no composition root declares it, and it "
        f"carries no owner-recorded exclusion" for module in undeclared
    )

    unproven = sorted(declared - real)
    findings.extend(
        f"declared edge {consumer} -> {producer} (consumes a capability {producer} provides) "
        f"is backed by no import in src/; it is either injection at the composition root or "
        f"it is not an edge" for consumer, producer in unproven
    )

    reachable = _reachable(list(_ENTRYPOINTS), sorted(real)) | set(_ENTRYPOINTS)
    unreachable = sorted(set(packages) - reachable)
    findings.extend(
        f"src/{module}/ is unreachable from an approved user-to-result entry point "
        f"({' or '.join(_ENTRYPOINTS)}) over real import edges" for module in unreachable
    )

    # Negative control. A scanner that cannot be made to fire reports "zero
    # unreachable" identically to an empty loop. Severing every import of
    # ``governance`` -- which 80+ files import -- must strand it.
    control_edges = sorted(edge for edge in real if edge[1] != "governance")
    control_reach = _reachable(list(_ENTRYPOINTS), control_edges) | set(_ENTRYPOINTS)
    control_fired = "governance" not in control_reach
    if not control_fired:
        findings.append(
            "negative control did not fire: severing every import of 'governance' left it "
            "reachable, so this scanner is not selective and its other findings are worthless"
        )

    execution_log = {
        "subject": "the real import graph of src/, not the registry's consumes column",
        "packages_on_disk": packages,
        "declared_in_registry": sorted(registry.declarations),
        "undeclared_packages": undeclared,
        "declared_capability_edges": len(declared),
        "real_import_edges": len(real),
        "edges_unproven_by_import": [list(edge) for edge in unproven],
        "unreachable_over_real_edges": unreachable,
        "entry_points": list(_ENTRYPOINTS),
        "staged": "reported UNVERIFIABLE, not FAIL, pending the owner decision",
    }
    negative_control = {
        "probe": "sever every real import edge whose target is 'governance', then re-run reachability",
        "expected": "governance becomes unreachable from the approved entry points",
        "fired": control_fired,
        "why": (
            "a reachability scanner that cannot be made to strand an 80-importer module "
            "reports 'zero unreachable' identically to an empty loop"
        ),
    }
    evidence = _standard_evidence(ctx, execution_log, negative_control)

    if not findings:
        return ok(
            "every first-party package under src/ is declared and reachable from an approved "
            "entry point over real import edges",
            evidence,
        )

    # Staged: enumerate, do not fail. See the docstring.
    return undecided(
        f"{len(findings)} composition-reachability findings enumerated against the real import "
        f"graph ({len(undeclared)} undeclared package(s), {len(unproven)} declared edge(s) "
        f"unproven by import, {len(unreachable)} unreachable module(s)). Reported as "
        f"UNVERIFIABLE rather than FAIL pending the owner decision on which are real gaps and "
        f"which are composition-root injection; see evidence for the full list. "
        + " | ".join(findings),
        evidence,
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register these six.
CHECKS_AUDIT_FOLLOWUP: dict[tuple[str, str], Check] = {
    ("GATE-D1-09", "A3"): d1_09_a3,
    ("GATE-D2-10", "A1"): d2_10_a1,
    ("GATE-D2-10", "A2"): d2_10_a2,
    ("GATE-D2-10", "A4"): d2_10_a4,
    ("GATE-D2-13", "A2"): d2_13_a2,
    ("GATE-D2-13", "A4"): d2_13_a4,
    ("GATE-D3-23", "A4"): d3_23_a4,
}
