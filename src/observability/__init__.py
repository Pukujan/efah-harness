"""EFAH module: observability. Contract EFAH-CONTRACT-001 v1.1 Sections 5, 23.

Correlated OpenTelemetry traces and the blinded model-identity policy shared by
the API, the read projections, and the Plane projection.
"""

from observability.identity import (
    ALIAS_PATTERN,
    ProtectedIdentityLeak,
    assert_alias_only,
    is_alias,
    matched_model_identifier,
    matched_vendor_token,
    scan_for_leaks,
)
from observability.spans import (
    REQUIRED_BY_KIND,
    REQUIRED_CORRELATION_FIELDS,
    Correlation,
    IncompleteCorrelation,
    SpanKindName,
    current_trace_id,
    efah_span,
    format_span_id,
    format_trace_id,
)

__all__ = [
    "ALIAS_PATTERN",
    "REQUIRED_BY_KIND",
    "REQUIRED_CORRELATION_FIELDS",
    "Correlation",
    "IncompleteCorrelation",
    "ProtectedIdentityLeak",
    "SpanKindName",
    "assert_alias_only",
    "current_trace_id",
    "efah_span",
    "format_span_id",
    "format_trace_id",
    "is_alias",
    "matched_model_identifier",
    "matched_vendor_token",
    "scan_for_leaks",
]
