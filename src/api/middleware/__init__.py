"""The Section 11.4 middleware stack, in one place with its order stated.

Contract Section 11.4 lists eleven concerns. Every one is implemented here:

=================================================  ==========================
Concern                                            Module
=================================================  ==========================
authentication and authorization                   ``auth``
human, service, and alias identity                 ``auth``
contract/project version binding                   ``version_binding``
correlation and trace IDs                          ``correlation``
schema validation                                  ``schema_validation``
request provenance                                 ``provenance``
rate and concurrency controls                      ``throttle``
input limits                                       ``limits``
audit logging                                      ``audit``
prompt-injection and untrusted-content boundaries  ``untrusted``
=================================================  ==========================

**Order is a correctness property, not a preference.** It reads outermost to
innermost:

1. ``correlation`` -- first, so everything below it, including a rejection, has
   a correlation id and a span to hang off;
2. ``boundary`` -- next, so a typed refusal raised by any middleware below is
   rendered as its contract state instead of a bare 500 (Starlette's exception
   handlers only wrap the router, not the middleware chain);
3. ``audit`` -- next, so it records the outcome of every layer beneath it,
   including the ones that reject;
4. ``limits`` -- before the body is read anywhere, so an oversized body is
   refused rather than buffered, and the body is captured exactly once;
5. ``auth`` -- identity resolved before any policy that depends on it;
6. ``version_binding`` -- a stale-contract request is stopped before any work;
7. ``throttle`` -- keyed on the now-known principal, before expensive layers;
8. ``schema_validation`` -- envelope checked before content is scanned;
9. ``untrusted`` -- subversion refused before provenance records it as accepted;
10. ``provenance`` -- records what was *actually* accepted, innermost.

:data:`MIDDLEWARE_ORDER` states it as data so a test can assert the wiring
matches, rather than the order silently drifting during a refactor.
"""

from __future__ import annotations

from typing import Any, Final

from api.middleware.audit import AuditMiddleware, AuditSink
from api.middleware.auth import AuthenticationMiddleware, TokenRegistry, requires
from api.middleware.boundary import TypedErrorBoundaryMiddleware
from api.middleware.correlation import CorrelationMiddleware
from api.middleware.limits import InputLimitsMiddleware, captured_body
from api.middleware.provenance import ProvenanceMiddleware
from api.middleware.schema_validation import SchemaValidationMiddleware
from api.middleware.throttle import ThrottleMiddleware
from api.middleware.untrusted import UntrustedContentMiddleware
from api.middleware.version_binding import VersionBindingMiddleware

#: Outermost first.
MIDDLEWARE_ORDER: Final = (
    CorrelationMiddleware,
    TypedErrorBoundaryMiddleware,
    AuditMiddleware,
    InputLimitsMiddleware,
    AuthenticationMiddleware,
    VersionBindingMiddleware,
    ThrottleMiddleware,
    SchemaValidationMiddleware,
    UntrustedContentMiddleware,
    ProvenanceMiddleware,
)

#: Contract Section 11.4's eleven bullets, for the completeness test.
SECTION_11_4_CONCERNS: Final = (
    "authentication",
    "authorization",
    "human_service_and_alias_identity",
    "contract_project_version_binding",
    "correlation_and_trace_ids",
    "schema_validation",
    "request_provenance",
    "rate_and_concurrency_controls",
    "input_limits",
    "audit_logging",
    "prompt_injection_and_untrusted_content_boundaries",
)

#: Which middleware discharges which concern. Asserted by the contract test.
CONCERN_COVERAGE: Final[dict[str, str]] = {
    "authentication": "AuthenticationMiddleware",
    "authorization": "AuthenticationMiddleware",
    "human_service_and_alias_identity": "AuthenticationMiddleware",
    "contract_project_version_binding": "VersionBindingMiddleware",
    "correlation_and_trace_ids": "CorrelationMiddleware",
    "schema_validation": "SchemaValidationMiddleware",
    "request_provenance": "ProvenanceMiddleware",
    "rate_and_concurrency_controls": "ThrottleMiddleware",
    "input_limits": "InputLimitsMiddleware",
    "audit_logging": "AuditMiddleware",
    "prompt_injection_and_untrusted_content_boundaries": "UntrustedContentMiddleware",
}


def install_middleware(app: Any, *, options: dict[type, dict[str, Any]] | None = None) -> None:
    """Add the stack to *app* in the documented order.

    Starlette wraps the most recently added middleware outermost, so
    :data:`MIDDLEWARE_ORDER` (outermost first) is added in reverse.
    """
    options = options or {}
    for middleware in reversed(MIDDLEWARE_ORDER):
        app.add_middleware(middleware, **options.get(middleware, {}))


__all__ = [
    "AuditMiddleware",
    "AuditSink",
    "AuthenticationMiddleware",
    "CONCERN_COVERAGE",
    "CorrelationMiddleware",
    "InputLimitsMiddleware",
    "MIDDLEWARE_ORDER",
    "ProvenanceMiddleware",
    "SECTION_11_4_CONCERNS",
    "SchemaValidationMiddleware",
    "ThrottleMiddleware",
    "TokenRegistry",
    "TypedErrorBoundaryMiddleware",
    "UntrustedContentMiddleware",
    "VersionBindingMiddleware",
    "captured_body",
    "install_middleware",
    "requires",
]
