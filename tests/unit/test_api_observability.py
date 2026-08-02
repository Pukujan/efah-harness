"""Correlated traces and the exporter endpoint policy (contract Section 23).

Section 23 makes correlation the requirement, not tracing: a span that omits
``run_id`` cannot be joined to the run it describes, so it proves nothing. And
``environments.yaml`` forbids exporting to another project's collector, which is
a security property, not a preference.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from integrations.otel import (
    DEFAULT_OTLP_ENDPOINT,
    DEFAULT_PHOENIX_URL,
    FORBIDDEN_PORTS,
    ForbiddenExporterEndpoint,
    OtelSettings,
    check_endpoint_allowed,
    install_tracer_provider,
    reset_tracer_provider,
)
from integrations.pack import load_pack
from observability.identity import (
    ProtectedIdentityLeak,
    assert_alias_only,
    is_alias,
    matched_model_identifier,
    scan_for_leaks,
)
from observability.spans import (
    REQUIRED_BY_KIND,
    REQUIRED_CORRELATION_FIELDS,
    Correlation,
    IncompleteCorrelation,
    SpanKindName,
    efah_span,
)


@pytest.fixture
def recorder():
    """Install a real provider with an in-memory exporter, then take it back."""
    reset_tracer_provider()
    exporter = InMemorySpanExporter()
    provider = install_tracer_provider(
        OtelSettings(synchronous_export=True), exporter=exporter
    )
    yield exporter, provider
    reset_tracer_provider()


# --------------------------------------------------------- endpoint policy


@pytest.mark.parametrize("port", sorted(FORBIDDEN_PORTS))
def test_another_projects_collector_is_refused(port: int) -> None:
    """environments.yaml: 6006/4317/4318 belong to cortex-*, tailnet-reachable."""
    with pytest.raises(ForbiddenExporterEndpoint):
        check_endpoint_allowed(f"http://localhost:{port}")
    with pytest.raises(ForbiddenExporterEndpoint):
        OtelSettings(endpoint=f"http://localhost:{port}")
    with pytest.raises(ForbiddenExporterEndpoint):
        OtelSettings(phoenix_url=f"http://localhost:{port}")


def test_the_efah_dedicated_endpoint_is_allowed() -> None:
    settings = OtelSettings()
    assert settings.endpoint == DEFAULT_OTLP_ENDPOINT == "http://localhost:4319"
    assert settings.phoenix_url == DEFAULT_PHOENIX_URL == "http://localhost:6007"
    assert settings.grpc_target == "localhost:4319"


def test_settings_come_from_the_pack_not_from_a_literal() -> None:
    settings = OtelSettings.from_pack(load_pack("project-pack"))
    assert settings.endpoint == "http://localhost:4319"
    assert settings.phoenix_url == "http://localhost:6007"


# ------------------------------------------------------ correlation fields


def test_section_23_field_list_is_complete() -> None:
    assert len(REQUIRED_CORRELATION_FIELDS) == 12
    declared = set(Correlation.__dataclass_fields__) | {"trace_id"}
    assert set(REQUIRED_CORRELATION_FIELDS) == declared


def test_a_span_missing_a_required_field_is_refused(recorder) -> None:
    """An uncorrelated span is not evidence, so it is not opened."""
    with pytest.raises(IncompleteCorrelation) as excinfo:
        with efah_span(
            "bad", kind=SpanKindName.MODEL_CALL, correlation=Correlation(project_id="P")
        ):
            pass
    assert "run_id" in excinfo.value.missing
    assert "model_alias" in excinfo.value.missing


@pytest.mark.parametrize("kind", list(SpanKindName))
def test_every_emitter_declares_what_it_must_carry(kind: SpanKindName) -> None:
    """Section 23 names seven emitters; each needs a stated minimum."""
    assert kind in REQUIRED_BY_KIND
    assert REQUIRED_BY_KIND[kind]


def test_a_complete_span_carries_all_twelve_fields(recorder) -> None:
    exporter, _ = recorder
    correlation = Correlation(
        project_id="EFAH-001",
        task_id="T-1",
        work_unit_id="WU-1",
        run_id="R-1",
        model_alias="judge-j03",
        role="judge",
        terminus_commit="tc",
        repository_commit="rc",
        evaluation_id="EV-1",
        oracle_version="1.0",
    )
    with efah_span("x", kind=SpanKindName.EVALUATION, correlation=correlation):
        pass
    attributes = exporter.get_finished_spans()[-1].attributes
    for field in REQUIRED_CORRELATION_FIELDS:
        assert f"efah.{field}" in attributes, field
    assert len(attributes["efah.trace_id"]) == 32


def test_trace_id_is_derived_from_the_span_not_supplied(recorder) -> None:
    """A caller-supplied trace id is a claim; Section 23 wants evidence."""
    assert "trace_id" not in Correlation.__dataclass_fields__
    exporter, _ = recorder
    with efah_span(
        "x", kind=SpanKindName.PROJECT, correlation=Correlation(project_id="P", run_id="R")
    ) as span:
        expected = format(span.get_span_context().trace_id, "032x")
    assert exporter.get_finished_spans()[-1].attributes["efah.trace_id"] == expected


def test_a_failing_span_is_recorded_as_failing(recorder) -> None:
    """A trace showing only successes is not evidence."""
    exporter, _ = recorder
    with pytest.raises(ValueError):
        with efah_span(
            "x", kind=SpanKindName.TASK,
            correlation=Correlation(project_id="P", run_id="R", task_id="T"),
        ):
            raise ValueError("boom")
    span = exporter.get_finished_spans()[-1]
    assert span.status.status_code is StatusCode.ERROR
    assert span.events


def test_a_span_cannot_carry_a_real_model_identity() -> None:
    """Section 11.2: a span is an audit record."""
    with pytest.raises(ProtectedIdentityLeak):
        Correlation(project_id="P", run_id="R", model_alias="claude-opus-4-1-20250805")


def test_child_spans_inherit_the_parents_ids() -> None:
    parent = Correlation(project_id="P", run_id="R")
    child = parent.merged(task_id="T", role=None)
    assert child.project_id == "P" and child.run_id == "R" and child.task_id == "T"
    assert child.role is None


# ----------------------------------------------------------- alias policy


@pytest.mark.parametrize(
    "alias", ["implementer-i12", "judge-j03", "holdout-h01", "researcher-r17"]
)
def test_real_pack_aliases_are_accepted(alias: str) -> None:
    assert is_alias(alias)
    assert assert_alias_only(alias, field="x") == alias


@pytest.mark.parametrize(
    "identity",
    [
        "claude-opus-4-1-20250805",
        "gpt-4o",
        "gemini-2.5-pro",
        "o3-mini",
        "anthropic/claude-sonnet-4",
        "meta-llama/Llama-3.1-70B",
    ],
)
def test_real_model_identifiers_are_detected(identity: str) -> None:
    assert matched_model_identifier(identity) is not None
    with pytest.raises(ProtectedIdentityLeak):
        assert_alias_only(identity, field="model_alias")


def test_an_unrecognised_shape_is_treated_as_unsafe() -> None:
    """Guessing wrong here writes a leak into an immutable commit."""
    with pytest.raises(ProtectedIdentityLeak):
        assert_alias_only("some-internal-name", field="model_alias")


def test_an_unset_alias_is_not_a_leak() -> None:
    assert assert_alias_only(None, field="x") is None
    assert assert_alias_only("", field="x") is None


def test_strict_scan_catches_prose_that_the_projection_scan_allows() -> None:
    """GATE-D1-06 A1 scans agent-visible payloads, where prose is not allowed."""
    payload = {"prompt": "you are like Claude, but better"}
    assert scan_for_leaks(payload) == []
    assert scan_for_leaks(payload, strict=True)
