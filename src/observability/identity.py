"""Blinded model-identity policy (contract Sections 11.2, 12.3, 17.2).

Real vendor/model identity lives in the isolated protected TerminusDB instance
and nowhere else. Everything that leaves the control plane -- telemetry spans,
dashboard read projections, the Plane projection, API responses -- carries the
*alias* only.

This module is the single place that decides "is this string a real model
identity?". It lives under ``observability`` because the same question has to be
answered for a span attribute, a projection field, and an audit record, and
three copies of the answer would drift apart. It is pure policy: no I/O, no
network, no vendor SDK.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: Aliases in ``model-policy.yaml`` are ``<role-word>-<letter><two digits>``:
#: ``implementer-i12``, ``judge-j03``, ``holdout-h01``.
ALIAS_PATTERN: Final = re.compile(r"^[a-z][a-z0-9]*-[a-z]\d{2}$")

#: Substrings that identify a real vendor, family, or model. Matched
#: case-insensitively against a normalised form of the candidate value, so
#: ``Claude_Opus_4`` and ``claude-opus-4`` are both caught.
VENDOR_TOKENS: Final = frozenset(
    {
        "anthropic",
        "claude",
        "opus",
        "sonnet",
        "haiku",
        "openai",
        "gpt",
        "chatgpt",
        "o1preview",
        "o3mini",
        "codex",
        "google",
        "gemini",
        "palm",
        "vertexai",
        "meta",
        "llama",
        "mistral",
        "mixtral",
        "codestral",
        "cohere",
        "command r",
        "deepseek",
        "qwen",
        "kimi",
        "glm",
        "minimax",
        "yi34b",
        "grok",
        "xai",
        "bedrock",
        "azureopenai",
        "titan",
        "nova",
        "phi3",
        "gemma",
        "falcon",
        "perplexity",
        "sonar",
    }
)

#: Fields that reveal relative standing between agents. Contract Section 12.3:
#: no agent receives another agent's prestige ranking or cost tier.
RANKING_FIELDS: Final = frozenset(
    {
        "prestige",
        "prestige_rank",
        "prestige_ranking",
        "cost_tier",
        "price_tier",
        "cost_per_token",
        "input_cost",
        "output_cost",
        "leaderboard_rank",
        "vendor",
        "provider",
        "model",
        "model_name",
        "model_id",
        "real_model",
        "real_model_id",
        "upstream_model",
    }
)


#: A *model identifier*, as opposed to a mention of a vendor in prose. The
#: distinction matters: the dashboard has to be able to render the contract's own
#: acceptance criteria, and GATE-D1-07 A2's claim is literally "No essential
#: module imports the Anthropic SDK or a Claude-specific client". Refusing to
#: display that sentence would be a scanner that fails safe into uselessness.
#:
#: So a bulk payload scan looks for the *shape* of an identifier -- a vendor or
#: family token bound to a version, a date stamp, or a provider path -- while
#: :func:`assert_alias_only` stays strict on the fields that are contractually
#: required to hold an alias and nothing else.
MODEL_IDENTIFIER_PATTERNS: Final = (
    # claude-opus-4-1-20250805, gpt-4o, gemini-2.5-pro, llama-3.1-70b, o3-mini
    re.compile(
        r"\b(claude|opus|sonnet|haiku|gpt|chatgpt|gemini|palm|llama|mistral|mixtral|"
        r"codestral|command-?r|deepseek|qwen|kimi|glm|minimax|grok|titan|nova|phi|"
        r"gemma|falcon|sonar)[-_ .]?v?\d",
        re.IGNORECASE,
    ),
    # anthropic/claude-..., meta-llama/Llama-3, us.anthropic.claude-...
    re.compile(
        r"\b(anthropic|openai|google|meta-llama|mistralai|cohere|deepseek-ai|qwen|"
        r"xai|bedrock|vertex)[/.][\w.\-]+",
        re.IGNORECASE,
    ),
    # o1-preview / o3-mini style bare reasoning-model ids
    re.compile(r"\bo[134]-(preview|mini|pro)\b", re.IGNORECASE),
)


def matched_model_identifier(value: object) -> str | None:
    """Return the matched identifier shape, or ``None``."""
    if not isinstance(value, str):
        return None
    for pattern in MODEL_IDENTIFIER_PATTERNS:
        found = pattern.search(value)
        if found:
            return found.group(0)
    return None


class ProtectedIdentityLeak(RuntimeError):
    """A real vendor/model identity reached a surface that may only see aliases.

    Maps to :class:`governance.states.DriftFinding.PROTECTED_ASSET_ACCESS` and,
    on a task, to :class:`governance.states.TaskState.FAILED_PROVENANCE`.
    """

    def __init__(self, field: str, matched: str) -> None:
        self.field = field
        self.matched = matched
        # The offending value itself is deliberately NOT interpolated: an
        # exception message ends up in logs, and logs are a surface too.
        super().__init__(
            f"PROTECTED_ASSET_ACCESS: field {field!r} carries a real model identity "
            f"(matched vendor token {matched!r}); contract Section 11.2 permits aliases only"
        )


def _normalise(value: str) -> str:
    """Collapse separators so ``Claude_Opus-4`` and ``claudeopus4`` both match."""
    return re.sub(r"[^a-z0-9 ]+", "", value.lower())


def matched_vendor_token(value: object) -> str | None:
    """Return the vendor token a value reveals, or ``None`` if it reveals none."""
    if not isinstance(value, str):
        return None
    normalised = _normalise(value)
    for token in VENDOR_TOKENS:
        if token in normalised:
            return token
    return None


def is_alias(value: object) -> bool:
    """True when *value* is a well-formed blinded alias and leaks no vendor."""
    return (
        isinstance(value, str)
        and ALIAS_PATTERN.match(value) is not None
        and matched_vendor_token(value) is None
    )


def assert_alias_only(value: object, *, field: str) -> str | None:
    """Validate an alias-bearing field. Returns the alias, or ``None`` if unset.

    Raises :class:`ProtectedIdentityLeak` when the value names a real model, or
    when it is a non-empty string that is not alias-shaped -- an unrecognised
    shape is treated as unsafe rather than waved through, because the failure
    mode of guessing wrong here is a permanent leak into an immutable commit.
    """
    if value is None or value == "":
        return None
    matched = matched_vendor_token(value)
    if matched is not None:
        raise ProtectedIdentityLeak(field, matched)
    if not isinstance(value, str) or not ALIAS_PATTERN.match(value):
        raise ProtectedIdentityLeak(field, "not-alias-shaped")
    return value


def scan_for_leaks(payload: Any, *, path: str = "$", strict: bool = False) -> list[tuple[str, str]]:
    """Walk an arbitrary payload and report every protected-identity leak.

    Returns ``[(json_path, matched), ...]``. Two kinds of finding:

    * a *key* naming a ranking or real-identity field (Section 12.3 A5) --
      ``vendor``, ``model_id``, ``cost_tier``, and friends. The presence of the
      key is the violation regardless of its value;
    * a *value* shaped like a real model identifier.

    ``strict=True`` widens the value test from "looks like a model identifier"
    to "mentions a vendor at all". That is the right setting for GATE-D1-06 A1's
    scan of an **agent-visible payload** -- a prompt or task record has no
    legitimate reason to name a vendor. It is the wrong setting for a dashboard
    projection, which must be able to render the contract's own text.
    """
    findings: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if str(key).lower() in RANKING_FIELDS:
                findings.append((child, f"ranking-or-identity-field:{key}"))
            findings.extend(scan_for_leaks(value, path=child, strict=strict))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            findings.extend(scan_for_leaks(value, path=f"{path}[{index}]", strict=strict))
    else:
        matched = (
            matched_vendor_token(payload) if strict else matched_model_identifier(payload)
        )
        if matched is not None:
            findings.append((path, matched))
    return findings
