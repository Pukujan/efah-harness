"""Per-request context (contract Section 11.4).

Middleware resolves identity, correlation, and version binding once and puts the
result here. Controllers read it. Nothing else in the request path re-derives
it, because two derivations of "who is calling" is one derivation too many.

Backed by ``contextvars`` so a span emitted deep inside a controller can pick up
the correlation ids without every function signature growing a parameter.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from observability.identity import assert_alias_only


class IdentityKind(StrEnum):
    """Contract Section 11.4: human, service, and alias identity."""

    HUMAN = "human"
    SERVICE = "service"
    ALIAS = "alias"
    ANONYMOUS = "anonymous"


class Scope(StrEnum):
    """Authorisation scopes. Deliberately coarse and closed.

    ``OWNER_DECIDE`` is separate from ``PROJECT_WRITE``: answering a typed owner
    blocker is an owner act, and a service credential that can start runs must
    not thereby be able to answer on the owner's behalf.
    """

    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    TASK_READ = "task:read"
    TASK_WRITE = "task:write"
    EVALUATION_READ = "evaluation:read"
    DEPENDENCY_READ = "dependency:read"
    CONTRACT_APPROVE = "contract:approve"
    CONTRACT_REVIEW = "contract:review"
    OWNER_DECIDE = "owner:decide"


OWNER_SCOPES: frozenset[Scope] = frozenset(Scope)
SERVICE_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.PROJECT_READ,
        Scope.PROJECT_WRITE,
        Scope.TASK_READ,
        Scope.TASK_WRITE,
        Scope.EVALUATION_READ,
        Scope.DEPENDENCY_READ,
        Scope.CONTRACT_REVIEW,
    }
)
#: A worker session identifies by alias and may read and submit, never approve.
ALIAS_SCOPES: frozenset[Scope] = frozenset(
    {Scope.PROJECT_READ, Scope.TASK_READ, Scope.TASK_WRITE, Scope.DEPENDENCY_READ}
)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    ``alias`` is validated on construction: a principal identified by a real
    model name would put a vendor identity into every audit record it touches
    (Section 11.2).
    """

    kind: IdentityKind
    subject: str
    scopes: frozenset[Scope] = frozenset()
    alias: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        assert_alias_only(self.alias, field="principal.alias")

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    def audit_identity(self) -> dict[str, str]:
        """The identity fields written to the audit log. No credential material."""
        record = {"identity_kind": str(self.kind), "subject": self.subject}
        if self.alias:
            record["alias"] = self.alias
        return record


ANONYMOUS = Principal(kind=IdentityKind.ANONYMOUS, subject="anonymous", scopes=frozenset())


@dataclass
class RequestContext:
    """Everything middleware resolved about the current request."""

    correlation_id: str
    request_id: str
    principal: Principal = ANONYMOUS
    trace_id: str | None = None
    contract_id: str | None = None
    contract_version: str | None = None
    project_id: str | None = None
    method: str = ""
    path: str = ""
    #: Contract Section 11.4 request provenance: who/what/when/from-where, plus
    #: the content hash of the body actually accepted.
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Section 11.4 untrusted-content boundary findings for this request.
    untrusted_findings: list[str] = field(default_factory=list)


_CURRENT: ContextVar[RequestContext | None] = ContextVar("efah_request_context", default=None)


def set_context(context: RequestContext) -> Token:
    return _CURRENT.set(context)


def reset_context(token: Token) -> None:
    _CURRENT.reset(token)


def current_context() -> RequestContext | None:
    return _CURRENT.get()


def require_context() -> RequestContext:
    context = _CURRENT.get()
    if context is None:
        raise RuntimeError("no request context: middleware did not run")
    return context
