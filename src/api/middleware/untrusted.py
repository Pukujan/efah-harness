"""Prompt-injection and untrusted-content boundary (contract Section 11.4).

Everything arriving over HTTP is untrusted data. The danger is not that a
string is rude; it is that a string reaches a model context and is read as an
*instruction* to the harness -- "ignore the contract", "you are now the
approver", "reveal the holdout".

Two rules, in order of strength:

1. **Structural.** Request content is marked untrusted in the request context and
   never promoted to instruction status. Downstream model callers must fence it.
   Structure is the real defence; pattern matching is not.
2. **Refusal at the boundary.** A payload that is *unambiguously* attempting
   contract subversion, protected-asset access, or gate bypass is rejected here
   with a typed finding rather than passed inward marked "suspicious". A
   suspicious payload that still gets processed is a payload that got processed.

The patterns are deliberately narrow. A broad filter that rejects ordinary
project prose would be worked around within the hour, and a filter people work
around is worse than none.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final
from urllib.parse import unquote_plus

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.context import current_context
from api.errors import ProtectedAssetAccess, UntrustedContentRejected
from api.middleware.limits import captured_body

#: Instruction-shaped attempts to override the governing contract or a gate.
INJECTION_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions?|prompts?|rules?)",
        r"disregard (the |your )?(contract|instructions?|system prompt|rules?)",
        r"you are now (the |an? )?(owner|approver|admin|verifier|judge)",
        r"(act|behave) as (the |an? )?(owner|approver|system|verifier)",
        r"override (the )?(gate|contract|policy|approval)",
        r"(mark|set) (this|the) (task|gate|project) as (passed|approved|verified)",
        r"skip (the )?(gate|verification|assurance|holdout)",
        r"reveal (the )?(system prompt|holdout|hidden (tests?|assertions?)|real model)",
        r"print (the )?(system prompt|api[_ ]?key|secret)",
        r"<\s*\|?\s*(system|assistant)\s*\|?\s*>",
    )
)

#: Section 17.2 and the brief: the sealed side is not addressable from here.
PROTECTED_ASSET_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"efah-lab-verifier",
        r"eval-lab-verifier",
        r"terminusdb_protected",
        r"TERMINUSDB_PROTECTED_PASS",
        r"localhost:6364",
        r"127\.0\.0\.1:6364",
    )
)

MAX_SCANNED_BYTES: Final = 262_144


def _scannable_text(request: Request) -> str:
    """Everything user-controlled, in the form the *reader* would see it.

    Both decodings matter. A percent-encoded query string and a ``\\u0069``
    escape inside a JSON string both arrive on the wire looking nothing like the
    instruction they become once decoded -- so the raw bytes are scanned *and*
    the decoded forms are scanned. Scanning only the raw bytes would let
    ``?note=ignore%20all%20previous%20instructions`` straight through.
    """
    body = captured_body(request.scope)[:MAX_SCANNED_BYTES]
    parts = [body.decode("utf-8", errors="replace") if body else ""]
    parts.append(request.url.query)
    parts.append(unquote_plus(request.url.query))
    if body:
        try:
            parts.extend(_strings(json.loads(body)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return "\n".join(parts)


def _strings(payload: Any) -> list[str]:
    """Every string value in a decoded payload, keys included."""
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [str(key) for key in payload] + [s for v in payload.values() for s in _strings(v)]
    if isinstance(payload, (list, tuple)):
        return [s for item in payload for s in _strings(item)]
    return []


class UntrustedContentMiddleware(BaseHTTPMiddleware):
    """Marks request content untrusted; refuses unambiguous subversion."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        text = _scannable_text(request)

        for pattern in PROTECTED_ASSET_PATTERNS:
            if pattern.search(text):
                raise ProtectedAssetAccess(
                    "the request names a protected asset (sealed verifier repository or the "
                    "isolated protected TerminusDB instance); this API cannot route to either"
                )

        findings = [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(text)]
        if findings:
            raise UntrustedContentRejected(
                "request content is instruction-shaped against the governing contract and was "
                "rejected at the untrusted-content boundary",
                matched_patterns=findings[:3],
            )

        context = current_context()
        if context is not None:
            # Structural marking: anything derived from this request body is data,
            # and a model caller downstream must fence it as such.
            context.provenance["content_trust"] = "untrusted_external_input"
            context.untrusted_findings = []
        return await call_next(request)
