"""Correlated EFAH spans (contract Section 23).

> Every project, task, model call, retrieval, tool call, evaluation, and gate
> MUST emit correlated OpenTelemetry traces.

"Correlated" is the whole requirement: a span that omits ``run_id`` cannot be
joined to the run it came from, so the trace proves nothing. This module makes
the twelve minimum correlation fields the *only* way to open a span, and refuses
a span whose ``model_alias`` carries a real vendor identity (Section 11.2).

``trace_id`` is not accepted as an input -- it is read back off the live span
context, because a caller-supplied trace id is a claim, and the point of Section
23 is that the trace is evidence.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from governance.envelope import CONTRACT_VERSION
from integrations.otel import installed_provider
from observability.identity import assert_alias_only

#: Contract Section 23 "minimum correlation fields", in contract order.
REQUIRED_CORRELATION_FIELDS: Final = (
    "project_id",
    "contract_version",
    "task_id",
    "work_unit_id",
    "run_id",
    "model_alias",
    "role",
    "terminus_commit",
    "repository_commit",
    "evaluation_id",
    "oracle_version",
    "trace_id",
)

ATTRIBUTE_PREFIX: Final = "efah."


class SpanKindName(StrEnum):
    """The seven emitters contract Section 23 enumerates."""

    PROJECT = "project"
    TASK = "task"
    MODEL_CALL = "model_call"
    RETRIEVAL = "retrieval"
    TOOL_CALL = "tool_call"
    EVALUATION = "evaluation"
    GATE = "gate"
    #: Not in the Section 23 list, but every API request is the entry point that
    #: the seven hang off; without it the others have no parent.
    API_REQUEST = "api_request"


class IncompleteCorrelation(ValueError):
    """A span was opened without a field Section 23 requires."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "contract Section 23 requires every span to carry the minimum correlation "
            f"fields; missing or empty: {', '.join(missing)}"
        )


@dataclass(frozen=True)
class Correlation:
    """The Section 23 correlation set for one span.

    Every field is optional at construction because not every emitter has every
    id -- a retrieval span has no ``evaluation_id`` -- but :meth:`require`
    states exactly which ones a given emitter must carry, and the span helper
    calls it. Silent omission is what Section 23 exists to prevent.
    """

    project_id: str | None = None
    contract_version: str = CONTRACT_VERSION
    task_id: str | None = None
    work_unit_id: str | None = None
    run_id: str | None = None
    model_alias: str | None = None
    role: str | None = None
    terminus_commit: str | None = None
    repository_commit: str | None = None
    evaluation_id: str | None = None
    oracle_version: str | None = None

    def __post_init__(self) -> None:
        # Section 11.2: a span is an audit record. Aliases only.
        assert_alias_only(self.model_alias, field="model_alias")

    def merged(self, **overrides: Any) -> Correlation:
        """Child spans inherit the parent's ids and override what they know."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f, None)]
        if missing:
            raise IncompleteCorrelation(missing)

    def attributes(self) -> dict[str, str]:
        """Render as ``efah.*`` span attributes, dropping unset fields.

        ``trace_id`` is absent here on purpose: it is stamped from the live span
        context by :func:`efah_span`.
        """
        return {
            f"{ATTRIBUTE_PREFIX}{key}": str(value)
            for key, value in asdict(self).items()
            if value is not None and value != ""
        }


#: Which correlation fields each emitter must carry. Derived from Section 23:
#: the field list is a minimum, but a model-call span with no ``model_alias``
#: or a gate span with no ``project_id`` cannot be correlated to anything.
REQUIRED_BY_KIND: Final[dict[SpanKindName, tuple[str, ...]]] = {
    SpanKindName.PROJECT: ("project_id", "contract_version", "run_id"),
    SpanKindName.TASK: ("project_id", "contract_version", "run_id", "task_id"),
    SpanKindName.MODEL_CALL: ("project_id", "contract_version", "run_id", "model_alias", "role"),
    SpanKindName.RETRIEVAL: ("project_id", "contract_version", "run_id"),
    SpanKindName.TOOL_CALL: ("project_id", "contract_version", "run_id"),
    SpanKindName.EVALUATION: ("project_id", "contract_version", "run_id", "evaluation_id"),
    SpanKindName.GATE: ("project_id", "contract_version", "run_id"),
    SpanKindName.API_REQUEST: ("contract_version",),
}


def tracer() -> trace.Tracer:
    """The tracer the OTel adapter installed, else the global one.

    Asking the adapter first rather than only ``trace.get_tracer`` matters:
    OpenTelemetry allows ``set_tracer_provider`` exactly once per process and
    silently ignores later calls, so a test that installs its own provider would
    otherwise export into whichever provider happened to be installed first.
    The adapter owns the provider (Section 5.1); this defers to it.
    """
    provider = installed_provider()
    if provider is not None:
        return provider.get_tracer("efah.control-plane")
    return trace.get_tracer("efah.control-plane")


def format_trace_id(span: Span) -> str:
    return format(span.get_span_context().trace_id, "032x")


def format_span_id(span: Span) -> str:
    return format(span.get_span_context().span_id, "016x")


@contextmanager
def efah_span(
    name: str,
    *,
    kind: SpanKindName,
    correlation: Correlation,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Open a Section 23 span, or refuse to open one that proves nothing.

    On an exception the span records the error and is marked ``ERROR`` before
    re-raising: an evidence trace that shows only successes is not evidence.
    """
    correlation.require(*REQUIRED_BY_KIND[kind])

    span_attributes: dict[str, Any] = dict(correlation.attributes())
    span_attributes[f"{ATTRIBUTE_PREFIX}span_kind"] = str(kind)
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        span_attributes[key if key.startswith(ATTRIBUTE_PREFIX) else f"{ATTRIBUTE_PREFIX}{key}"] = (
            value if isinstance(value, (str, int, float, bool)) else str(value)
        )

    with tracer().start_as_current_span(name, attributes=span_attributes) as span:
        # trace_id is derived, never supplied -- see module docstring.
        span.set_attribute(f"{ATTRIBUTE_PREFIX}trace_id", format_trace_id(span))
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        else:
            span.set_status(Status(StatusCode.OK))


def current_trace_id() -> str | None:
    """The active trace id as 32 hex chars, or ``None`` outside a span."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")
