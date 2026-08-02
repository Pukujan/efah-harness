"""The model router -- a deterministic policy service (contract Section 11.1).

It is *not* an orchestrator. It does not call models, does not hold a session,
and does not decide what work happens next. Given the Section 11.1 request shape
it returns **an alias and a configuration version**, and nothing that would let
the caller infer a vendor, a family, a prestige rank, or a cost tier
(Section 12.3).

Determinism is the property that makes it evidence-grade: the same request
against the same policy bytes and the same capability records returns the same
alias and the same ``decision_hash``, every time, in any process.

What it enforces, mechanically rather than by agent judgment:

* the role -> alias map from ``model-policy.yaml`` (never hardcoded here);
* ``role_incompatibilities`` -- ``implementer`` may not share an alias *or* a
  family with ``sealed_holdout_author`` or ``judge``, may not share a family
  with ``adversarial_critic``, and so on. A violation is ``ROLE_CONFLICT``;
* **the separations the contract states directly**, whether or not the pack
  declared a rule for them (:mod:`models.separation`). The pack's rule list and
  its alias map are both owner data in one file, so a pair the owner never wrote
  a rule for was silently unconstrained: measured, all five binding rules were
  edges from ``implementer``, leaving every assurance-to-assurance pair
  unchecked. §1.2 puts the contract above the pack, so §12.2/§12.4 are enforced
  from the contract side. All sixteen required edges hold on the current map, so
  this changes no routing today -- it means a regression cannot pass unnoticed;
* ``prohibited_models`` and ``degraded_at_pack_time``;
* DEC-002 gateway class, carried on the decision so the dispatch layer cannot
  pick the wrong one;
* ``availability_probe.required_before_first_dispatch`` -- with no capability
  record, the router refuses rather than assuming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governance.envelope import content_hash
from models.availability import CapabilityRegistry
from models.errors import (
    AvailabilityProbeRequiredError,
    ModelUnavailableError,
    ProhibitedModelError,
    RoleConflictError,
)
from models.policy import ModelPolicy, RoleModel, load_model_policy
from models.separation import Strength, evaluate

#: Ordered strongest to weakest; used only to pick a deterministic substitute.
_TIER_RANK = {"frontier": 0, "mid": 1, "cheap": 2, "unspecified": 3}


@dataclass(frozen=True)
class RoutingRequest:
    """The contract Section 11.1 request shape, verbatim."""

    role: str
    required_capabilities: tuple[str, ...] = ()
    prohibited_aliases: tuple[str, ...] = ()
    required_family_separation: bool = True
    risk_class: str = "standard"
    context_requirement: str | None = None
    availability_probe_required: bool = True

    def as_body(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "required_capabilities": list(self.required_capabilities),
            "prohibited_aliases": list(self.prohibited_aliases),
            "required_family_separation": self.required_family_separation,
            "risk_class": self.risk_class,
            "context_requirement": self.context_requirement,
            "availability_probe_required": self.availability_probe_required,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """What the router returns. Blinded by construction.

    There is no ``model``, ``family``, ``vendor``, ``tier``, or ``price`` field
    and there never may be one: this object is handed to worker sessions and
    written into task records (GATE-D1-06 A2). The dispatch layer resolves the
    alias through the protected identity store instead.
    """

    role: str
    alias: str
    gateway: str
    configuration_version: str
    policy_hash: str
    max_tokens_floor: int
    hard_floor_max_tokens: int
    max_retries: int
    timeout_seconds: int
    availability_verified_at: str | None
    substituted: bool
    reasons: tuple[str, ...] = ()
    decision_hash: str = ""

    def as_body(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "alias": self.alias,
            "gateway": self.gateway,
            "configuration_version": self.configuration_version,
            "policy_hash": self.policy_hash,
            "max_tokens_floor": self.max_tokens_floor,
            "hard_floor_max_tokens": self.hard_floor_max_tokens,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "availability_verified_at": self.availability_verified_at,
            "substituted": self.substituted,
            "reasons": list(self.reasons),
        }

    def with_hash(self) -> RoutingDecision:
        body = self.as_body()
        return RoutingDecision(**{**body, "reasons": self.reasons, "decision_hash": content_hash(body)})


@dataclass
class ModelRouter:
    """Deterministic policy service over a loaded :class:`ModelPolicy`."""

    policy: ModelPolicy = field(default_factory=load_model_policy)
    capabilities: CapabilityRegistry | None = None

    # -- separation ---------------------------------------------------------
    def role_separation_findings(self, role: str | None = None) -> list[str]:
        """Evaluate ``role_incompatibilities`` against the current alias map.

        The map is static owner data, so a violation is detectable without
        dispatching anything. ``should_differ_*`` rules are advisory and are
        reported with an ``advisory:`` prefix rather than raised.
        """
        findings: list[str] = []
        rules = (
            self.policy.incompatibilities_for(role)
            if role is not None
            else self.policy.incompatibilities
        )
        for rule in rules:
            rows = [self.policy.roles.get(name) for name in rule.roles]
            if any(row is None for row in rows):
                findings.append(f"{rule.rule} {rule.roles}: a named role is not mapped")
                continue
            prefix = "advisory: " if rule.is_advisory else ""
            aliases = {row.alias for row in rows}  # type: ignore[union-attr]
            families = {row.family for row in rows}  # type: ignore[union-attr]
            if rule.requires_distinct_alias and len(aliases) < len(rows):
                findings.append(
                    f"{prefix}{list(rule.roles)} must differ by agent but share an alias "
                    f"({rule.contract_ref})"
                )
            if rule.requires_distinct_family and len(families) < len(rows):
                findings.append(
                    f"{prefix}{list(rule.roles)} must differ by family but share one "
                    f"({rule.contract_ref})"
                )

        findings.extend(self._contract_separation_findings(role))
        return findings

    def _contract_separation_findings(self, role: str | None) -> list[str]:
        """Separations §12 states that the pack may not have written a rule for.

        The pack's rules are checked above; this checks the contract's. A
        ``CONDITIONAL`` clause ("where feasible", "where family bias is
        material") is reported as advisory, because deciding materiality is the
        owner's, not the router's.
        """
        findings: list[str] = []
        for edge in evaluate(self.policy):
            required = edge.required
            if role is not None and role not in (required.left, required.right):
                continue
            if edge.mechanized or edge.holds_on_the_current_map is not False:
                continue
            prefix = "advisory: " if required.strength is Strength.CONDITIONAL else ""
            findings.append(
                f"{prefix}[{required.left}, {required.right}] must differ by "
                f"{required.dimension.value} but share {edge.left_value!r} "
                f"({required.contract_ref}; the pack declares no rule for this pair)"
            )
        return findings

    def assert_role_separation(self, role: str | None = None) -> None:
        blocking = [f for f in self.role_separation_findings(role) if not f.startswith("advisory: ")]
        if blocking:
            raise RoleConflictError("role separation violated", detail=blocking)

    # -- routing ------------------------------------------------------------
    def route(self, request: RoutingRequest) -> RoutingDecision:
        row = self.policy.role(request.role)
        gateway = self.policy.gateway_routing.gateway_for_role(request.role)
        self.assert_role_separation(request.role)

        reasons: list[str] = [
            f"role_map:{request.role}",
            f"gateway_routing:{gateway}",
            "role_incompatibilities:satisfied",
        ]

        candidate = row
        substituted = False
        blockers = self._selection_blockers(row, request)
        if blockers:
            candidate = self._substitute(row, request)
            substituted = True
            reasons.append(f"substituted:{';'.join(blockers)}")

        verified_at = self._availability_check(candidate, request, reasons)

        client_req = self.policy.gateway_routing.client_requirements.get(gateway, {})
        # DEC-002: zero harness-level retry on the eval path; candidate work may
        # be retried with typed classification (Section 10.6).
        max_retries = (
            0 if gateway == "eval" else self.policy.retry_policy.max_retries_per_work_unit
        )
        timeout = int(client_req.get("sdk_timeout_seconds", 120))

        decision = RoutingDecision(
            role=request.role,
            alias=candidate.alias,
            gateway=gateway,
            configuration_version=self.policy.configuration_version,
            policy_hash=self.policy.policy_hash,
            max_tokens_floor=self.policy.request_policy.min_max_tokens_for_tool_calls,
            hard_floor_max_tokens=self.policy.request_policy.hard_floor_max_tokens,
            max_retries=max_retries,
            timeout_seconds=timeout,
            availability_verified_at=verified_at,
            substituted=substituted,
            reasons=tuple(reasons),
        )
        return decision.with_hash()

    # -- internals ----------------------------------------------------------
    def _selection_blockers(self, row: RoleModel, request: RoutingRequest) -> list[str]:
        blockers: list[str] = []
        if row.alias in request.prohibited_aliases:
            blockers.append("prohibited_alias")
        reason = self.policy.prohibition_reason(row.litellm_model)
        if reason is not None:
            blockers.append(f"prohibited_model:{reason}")
        if self.policy.is_degraded_at_pack_time(row.litellm_model):
            blockers.append("degraded_at_pack_time")
        if self.capabilities is not None and self.capabilities.is_known_unavailable(row.alias):
            blockers.append("empirically_unavailable")
        return blockers

    def _substitute(self, row: RoleModel, request: RoutingRequest) -> RoleModel:
        """Pick a replacement deterministically, preserving family separation.

        ``availability_probe.on_unavailable`` says
        ``select_declared_fallback_preserving_family_separation``. The pack
        declares no per-role fallback list, so the candidate pool is the declared
        alias inventory on the *same gateway class* -- the only models the owner
        has approved. Ordering is (tier distance, alias) so two processes cannot
        disagree.
        """
        forbidden_families = self._families_that_would_conflict(row.role)
        pool = [
            other
            for other in self.policy.roles.values()
            if other.gateway == row.gateway
            and other.alias != row.alias
            and other.alias not in request.prohibited_aliases
            and self.policy.prohibition_reason(other.litellm_model) is None
            and not self.policy.is_degraded_at_pack_time(other.litellm_model)
            and not (
                self.capabilities is not None
                and self.capabilities.is_known_unavailable(other.alias)
            )
            and not (request.required_family_separation and other.family in forbidden_families)
        ]
        if not pool:
            raise ModelUnavailableError(
                f"no approved alias remains for role {row.role!r} on gateway {row.gateway!r} "
                "that preserves family separation"
            )
        target_rank = _TIER_RANK.get(row.tier, 3)
        pool.sort(key=lambda o: (abs(_TIER_RANK.get(o.tier, 3) - target_rank), o.alias))
        chosen = pool[0]
        if self.policy.prohibition_reason(chosen.litellm_model):  # pragma: no cover - defensive
            raise ProhibitedModelError(f"substitute for {row.role!r} is prohibited")
        return chosen

    def _families_that_would_conflict(self, role: str) -> frozenset[str]:
        families: set[str] = set()
        for rule in self.policy.incompatibilities_for(role):
            if rule.is_advisory or not rule.requires_distinct_family:
                continue
            for other in rule.roles:
                if other == role:
                    continue
                counterpart = self.policy.roles.get(other)
                if counterpart is not None:
                    families.add(counterpart.family)
        return frozenset(families)

    def _availability_check(
        self, row: RoleModel, request: RoutingRequest, reasons: list[str]
    ) -> str | None:
        if not (
            request.availability_probe_required
            and self.policy.availability_probe.required_before_first_dispatch
        ):
            reasons.append("availability:not_required_by_request")
            return None
        if self.capabilities is None:
            raise AvailabilityProbeRequiredError(
                "availability_probe.required_before_first_dispatch is true but the router "
                "holds no capability registry; run models.availability.AvailabilityProbe first"
            )
        record = self.capabilities.get(row.alias)
        if record is None:
            raise AvailabilityProbeRequiredError(
                f"no ModelCapability record for alias {row.alias!r}; a static assumption of "
                "availability is not evidence"
            )
        if not record.available:
            raise ModelUnavailableError(
                f"alias {row.alias!r} was probed unavailable at {record.probed_at}"
            )
        reasons.append(f"availability:probed:{record.probed_at}")
        return record.probed_at
