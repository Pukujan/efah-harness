"""Typed view over ``project-pack/model-policy.yaml``.

Contract Sections 11.1 and 11.2. The routing table is owner data, not code: this
module parses it and refuses to guess. Nothing here hardcodes a role, an alias, a
vendor, or a gateway assignment -- change the pack and the router changes with
it.

Two invariants are checked at load time rather than at call time, because a pack
that contradicts itself must not be routable at all:

1. every role's ``gateway:`` field agrees with the ``gateway_routing``
   permitted-role lists (disagreement is ``FAILED_PROVENANCE``, DEC-002);
2. no role is mapped to a prohibited or pack-time-degraded model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from governance.envelope import content_hash
from models.errors import FailedProvenanceError, ProhibitedModelError

#: Walk up from ``src/models/policy.py`` to the worktree root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = _REPO_ROOT / "project-pack" / "model-policy.yaml"
DEFAULT_ENVIRONMENTS_PATH = _REPO_ROOT / "project-pack" / "environments.yaml"


@dataclass(frozen=True)
class RoleModel:
    """One row of the role -> alias map.

    ``litellm_model``, ``family`` and ``tier`` are *protected identity* fields
    (Section 11.2, Section 12.3). They exist here because this object is the
    dispatch-side view; they must never reach a task-facing payload. See
    :mod:`models.blinding`.
    """

    role: str
    alias: str
    litellm_model: str
    family: str
    gateway: str
    tier: str
    measured: dict[str, Any] = field(default_factory=dict)
    quirk: str | None = None
    runs_under_identity: str | None = None

    def blinded(self) -> dict[str, str]:
        """The only projection of this row an agent may ever see."""
        return {"role": self.role, "alias": self.alias, "gateway": self.gateway}


@dataclass(frozen=True)
class RoleIncompatibility:
    roles: tuple[str, ...]
    rule: str
    contract_ref: str | None = None

    @property
    def is_advisory(self) -> bool:
        """``should_differ_*`` is a preference; ``must_differ_*`` is a gate."""
        return self.rule.startswith("should_")

    @property
    def requires_distinct_alias(self) -> bool:
        return "by_agent" in self.rule

    @property
    def requires_distinct_family(self) -> bool:
        return "by_family" in self.rule


@dataclass(frozen=True)
class RequestPolicy:
    min_max_tokens_for_tool_calls: int
    hard_floor_max_tokens: int
    violation_state: str
    global_throttle_required: bool
    max_requests_per_minute: int
    min_interval_seconds: float
    throttle_scope: str
    unthrottled_fanout: str
    prefer_streaming: bool


@dataclass(frozen=True)
class GatewayRouting:
    permitted_roles: dict[str, tuple[str, ...]]
    client_requirements: dict[str, dict[str, Any]]
    violation_state: str

    def gateway_for_role(self, role: str) -> str:
        for gateway, roles in self.permitted_roles.items():
            if role in roles:
                return gateway
        raise FailedProvenanceError(
            f"role {role!r} has no declared gateway in model-policy.yaml -> gateway_routing; "
            "an undeclared role may not dispatch"
        )

    @property
    def gate_bearing_roles(self) -> frozenset[str]:
        return frozenset(self.permitted_roles.get("eval", ()))

    @property
    def candidate_roles(self) -> frozenset[str]:
        return frozenset(self.permitted_roles.get("production", ()))


@dataclass(frozen=True)
class RetryPolicy:
    classify_before_retry: bool
    max_retries_per_work_unit: int
    on_exhaustion: str
    gateway_level_retry_on_eval_path: str
    fallback_preserves_family_separation: bool
    failure_classes: tuple[str, ...]


@dataclass(frozen=True)
class AvailabilityProbePolicy:
    required_before_first_dispatch: bool
    probe_all_mapped_aliases: bool
    probe_max_tokens: int
    probe_includes_tool_call: bool
    on_unavailable: str
    record_as: str


@dataclass(frozen=True)
class SessionPolicy:
    fresh_per_invocation_worker_sessions: bool
    persistent_model_conversation_memory_default: bool
    durable_state_location: str
    chat_transcript_as_project_memory: str


@dataclass(frozen=True)
class ModelPolicy:
    """The parsed pack policy plus its content hash.

    ``configuration_version`` is what the router returns alongside an alias
    (Section 11.1) and what Section 18 records with every model run: it binds a
    routing decision to the exact bytes of the policy that produced it.
    """

    source_path: Path
    policy_hash: str
    schema_version: str
    contract_id: str
    contract_version: str
    roles: dict[str, RoleModel]
    prohibited_patterns: tuple[tuple[str, str], ...]
    degraded_models: frozenset[str]
    request_policy: RequestPolicy
    incompatibilities: tuple[RoleIncompatibility, ...]
    gateway_routing: GatewayRouting
    retry_policy: RetryPolicy
    availability_probe: AvailabilityProbePolicy
    session_policy: SessionPolicy
    authority_limits: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- identity -----------------------------------------------------------
    @property
    def configuration_version(self) -> str:
        return f"model-policy/{self.schema_version}+{self.policy_hash.removeprefix('sha256:')[:12]}"

    # -- lookups ------------------------------------------------------------
    def role(self, name: str) -> RoleModel:
        try:
            return self.roles[name]
        except KeyError:
            raise FailedProvenanceError(
                f"role {name!r} is not declared in model-policy.yaml -> aliases"
            ) from None

    def role_for_alias(self, alias: str) -> RoleModel:
        for row in self.roles.values():
            if row.alias == alias:
                return row
        raise FailedProvenanceError(f"alias {alias!r} is not declared in model-policy.yaml")

    @property
    def aliases(self) -> frozenset[str]:
        return frozenset(row.alias for row in self.roles.values())

    def prohibition_reason(self, litellm_model: str) -> str | None:
        """Return the recorded reason if this model may never be selected.

        ``*`` is the only wildcard. ``fnmatch`` is not used: real model ids here
        contain literal ``[`` and ``]`` (``[grok] grok-4.5``, ``[不稳定渠道] *``)
        and fnmatch would read those as character classes, so the broadest
        prohibition in the pack would silently match nothing.
        """
        for pattern, reason in self.prohibited_patterns:
            if pattern == litellm_model:
                return reason
            regex = "^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$"
            if re.match(regex, litellm_model):
                return reason
        return None

    def is_degraded_at_pack_time(self, litellm_model: str) -> bool:
        return litellm_model in self.degraded_models

    def incompatibilities_for(self, role: str) -> tuple[RoleIncompatibility, ...]:
        return tuple(rule for rule in self.incompatibilities if role in rule.roles)

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> ModelPolicy:
        path = Path(path) if path is not None else DEFAULT_POLICY_PATH
        raw_bytes = path.read_bytes()
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise FailedProvenanceError(f"{path} does not parse to a mapping")

        roles = {
            role: RoleModel(
                role=role,
                alias=entry["alias"],
                litellm_model=entry["litellm_model"],
                family=entry["family"],
                gateway=entry["gateway"],
                tier=entry.get("tier", "unspecified"),
                measured=dict(entry.get("measured") or {}),
                quirk=entry.get("quirk"),
                runs_under_identity=entry.get("runs_under_identity"),
            )
            for role, entry in (data.get("aliases") or {}).items()
        }

        routing_raw = data.get("gateway_routing") or {}
        gateway_routing = GatewayRouting(
            permitted_roles={
                name: tuple(cfg.get("permitted_roles") or ())
                for name, cfg in routing_raw.items()
                if isinstance(cfg, dict)
            },
            client_requirements={
                name: dict(cfg.get("client_requirements") or {})
                for name, cfg in routing_raw.items()
                if isinstance(cfg, dict)
            },
            violation_state=routing_raw.get("violation_state", "FAILED_PROVENANCE"),
        )

        rp = data.get("request_policy") or {}
        request_policy = RequestPolicy(
            min_max_tokens_for_tool_calls=int(rp["min_max_tokens_for_tool_calls"]),
            hard_floor_max_tokens=int(rp["hard_floor_max_tokens"]),
            violation_state=rp.get("violation_state", "FAILED_ORACLE"),
            global_throttle_required=bool(rp.get("global_throttle_required", True)),
            max_requests_per_minute=int(rp["global_throttle_max_requests_per_minute"]),
            min_interval_seconds=float(rp["global_throttle_min_interval_seconds"]),
            throttle_scope=rp.get("throttle_scope", "account_wide_not_per_model"),
            unthrottled_fanout=rp.get("unthrottled_fanout", "forbidden"),
            prefer_streaming=bool(rp.get("prefer_streaming", True)),
        )

        incompatibilities = tuple(
            RoleIncompatibility(
                roles=tuple(entry["roles"]),
                rule=entry["rule"],
                contract_ref=entry.get("contract_ref"),
            )
            for entry in (data.get("role_incompatibilities") or [])
        )

        rf = data.get("retry_and_fallback") or {}
        retry_policy = RetryPolicy(
            classify_before_retry=bool(rf.get("classify_before_retry", True)),
            max_retries_per_work_unit=int(rf.get("max_retries_per_work_unit", 0)),
            on_exhaustion=rf.get("on_exhaustion", "REWORK_REQUIRED"),
            gateway_level_retry_on_eval_path=rf.get("gateway_level_retry_on_eval_path", "forbidden"),
            fallback_preserves_family_separation=bool(
                rf.get("fallback_preserves_family_separation", True)
            ),
            failure_classes=tuple(rf.get("failure_classes") or ()),
        )

        ap = data.get("availability_probe") or {}
        availability_probe = AvailabilityProbePolicy(
            required_before_first_dispatch=bool(ap.get("required_before_first_dispatch", True)),
            probe_all_mapped_aliases=bool(ap.get("probe_all_mapped_aliases", True)),
            probe_max_tokens=int(ap.get("probe_max_tokens", 512)),
            probe_includes_tool_call=bool(ap.get("probe_includes_tool_call", True)),
            on_unavailable=ap.get("on_unavailable", "select_declared_fallback_preserving_family_separation"),
            record_as=ap.get("record_as", "ModelCapability"),
        )

        sp = data.get("session_policy") or {}
        session_policy = SessionPolicy(
            fresh_per_invocation_worker_sessions=bool(
                sp.get("fresh_per_invocation_worker_sessions", True)
            ),
            persistent_model_conversation_memory_default=bool(
                sp.get("persistent_model_conversation_memory_default", False)
            ),
            durable_state_location=sp.get("durable_state_location", "terminusdb_and_git_and_checkpoints"),
            chat_transcript_as_project_memory=sp.get("chat_transcript_as_project_memory", "forbidden"),
        )

        policy = cls(
            source_path=path,
            policy_hash=content_hash(raw_bytes),
            schema_version=str(data.get("schema_version", "0")),
            contract_id=str(data.get("contract_id", "")),
            contract_version=str(data.get("contract_version", "")),
            roles=roles,
            prohibited_patterns=tuple(
                (entry["model"], entry.get("reason", "unspecified"))
                for entry in (data.get("prohibited_models") or [])
            ),
            degraded_models=frozenset((data.get("degraded_at_pack_time") or {}).get("models") or ()),
            request_policy=request_policy,
            incompatibilities=incompatibilities,
            gateway_routing=gateway_routing,
            retry_policy=retry_policy,
            availability_probe=availability_probe,
            session_policy=session_policy,
            authority_limits=dict(data.get("authority_limits") or {}),
            raw=data,
        )
        policy.validate()
        return policy

    # -- self-consistency ---------------------------------------------------
    def validate(self) -> None:
        """Reject a pack that contradicts itself before anything can dispatch."""
        for role, row in self.roles.items():
            declared = self.gateway_routing.gateway_for_role(role)
            if declared != row.gateway:
                raise FailedProvenanceError(
                    f"role {role!r} declares gateway {row.gateway!r} on its alias entry but "
                    f"gateway_routing places it on {declared!r}; DEC-002 makes this ambiguity "
                    "unroutable"
                )
            reason = self.prohibition_reason(row.litellm_model)
            if reason is not None:
                raise ProhibitedModelError(
                    f"role {role!r} is mapped to prohibited model (reason: {reason})"
                )
            if self.is_degraded_at_pack_time(row.litellm_model):
                raise ProhibitedModelError(
                    f"role {role!r} is mapped to a model recorded as degraded at pack time"
                )


@lru_cache(maxsize=4)
def _cached_policy(path_str: str) -> ModelPolicy:
    return ModelPolicy.load(Path(path_str))


def load_model_policy(path: Path | str | None = None) -> ModelPolicy:
    """Load (and process-cache) the pack policy. The pack is read-only."""
    return _cached_policy(str(Path(path) if path is not None else DEFAULT_POLICY_PATH))


def load_environments(path: Path | str | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else DEFAULT_ENVIRONMENTS_PATH
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise FailedProvenanceError(f"{path} does not parse to a mapping")
    return data
