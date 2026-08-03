"""Role-separation coverage — what the *contract* requires, not what the pack declares.

``ModelRouter.role_separation_findings`` evaluates
``model-policy.yaml -> role_incompatibilities`` against the alias map. That is
the right check, and it passes today with zero findings. This module asks the
question one level up: **are those the right rules?**

The distinction matters because of the authority order in contract §1.2. The
alias map and the rule list are both owner data in the same file, so a role pair
the owner never wrote a rule for is silently unconstrained — the router cannot
report a violation of a rule that does not exist. The contract text, however, is
authority. §12.2, §12.4 and §17.4 state separations in prose; this module encodes
them as edges and reports which ones the pack declares, which it declares only
advisorily, and which it does not declare at all.

What the measurement found when this was written (see FINDING-006): all five
binding rules in the pack are edges from ``implementer``. It is a star, not a
mesh. No binding rule constrains any two assurance roles against each other, and
five roles are named in no rule at all. The consequence is concrete rather than
theoretical — ``visible_test_author``, ``sealed_holdout_author`` and
``contract_compliance_auditor`` are all family ``anthropic`` and FINDING-005
measured all three on one upstream channel, and nothing in the harness notices,
because every rule looks at the implementer instead of at them.

**This module reports; it does not rewrite the map.** The alias map is owner
data and FINDING-003 already established that the builder does not adjust it.
A missing *rule*, though, is not an owner preference — it is a contract clause
with no mechanization, which §13.4 calls out directly. So the requirement is
encoded here, the shortfall is measured, and the owner adjudicates which model
moves.

The ``family`` field this reasons over is a **label**, and FINDING-005 measured
that three differently-labelled Anthropic models resolve to one resold account
pool. A cross-family edge that holds on labels may not hold at the transport.
:func:`coverage_report` therefore reports family separation as
``label_verified`` rather than ``verified``, because that is what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from models.policy import ModelPolicy, load_model_policy


class Strength(StrEnum):
    """How hard the contract states the separation."""

    #: "MUST be distinct" / "MUST NOT". A shortfall is a contract gap.
    REQUIRED = "REQUIRED"
    #: "where feasible" / "where family bias is material". A shortfall is a
    #: finding to adjudicate, not automatically a violation.
    CONDITIONAL = "CONDITIONAL"


class Dimension(StrEnum):
    AGENT = "agent"
    FAMILY = "family"


@dataclass(frozen=True)
class RequiredSeparation:
    """One edge the contract requires, with the clause that requires it."""

    left: str
    right: str
    dimension: Dimension
    strength: Strength
    contract_ref: str
    rationale: str

    @property
    def pair(self) -> frozenset[str]:
        return frozenset({self.left, self.right})


#: Every separation the contract states, transcribed with its clause. Ordered by
#: the section that states it so the list can be diffed against the contract by
#: eye. Nothing here is inferred from the current alias map.
REQUIRED_SEPARATIONS: tuple[RequiredSeparation, ...] = (
    # -- §12.2 "Builder, holdout author, and final adjudicator MUST be distinct
    #    roles and agents." Three roles named in one sentence means three
    #    pairwise edges, not two. The pack declares the two that touch the
    #    implementer and omits the third.
    RequiredSeparation(
        "implementer", "sealed_holdout_author", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_builder_holdout_adjudicator_distinct",
        "the builder must not author what will judge it",
    ),
    RequiredSeparation(
        "implementer", "judge", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_builder_holdout_adjudicator_distinct",
        "the builder must not adjudicate its own disputes",
    ),
    RequiredSeparation(
        "sealed_holdout_author", "judge", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_builder_holdout_adjudicator_distinct",
        "the third edge of the same sentence: an adjudicator ruling on a "
        "holdout it wrote is reviewing itself",
    ),
    # -- §12.2 "The implementer MUST NOT access sealed holdouts, private
    #    mutants, or oracle internals." Authoring is the strongest form of
    #    access, so each producer of a sealed asset is an edge.
    RequiredSeparation(
        "implementer", "mutant_author", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_implementer_no_sealed_access",
        "authoring a mutant is knowing it",
    ),
    RequiredSeparation(
        "implementer", "oracle_author", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_implementer_no_sealed_access",
        "oracle internals are named in the same clause as holdouts and mutants",
    ),
    # -- §12.2 "A producing model MUST NOT be the sole reviewer or judge of its
    #    output." Every producer/reviewer pair, not only the critic.
    RequiredSeparation(
        "implementer", "adversarial_critic", Dimension.FAMILY, Strength.REQUIRED,
        "contract_12.2_producer_not_sole_reviewer",
        "the critic exists to refute the implementer",
    ),
    RequiredSeparation(
        "implementer", "integration_verifier", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_producer_not_sole_reviewer",
        "wiring is produced by the implementer and verified here; §14.4's "
        "walking-skeleton evidence rests on this edge",
    ),
    RequiredSeparation(
        "implementer", "visible_test_author", Dimension.AGENT, Strength.REQUIRED,
        "contract_14.3_test_first_behavior",
        "a test the implementer wrote is a test it already passes",
    ),
    RequiredSeparation(
        "implementer", "evidence_auditor", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_producer_not_sole_reviewer",
        "the auditor checks evidence the implementer produced",
    ),
    RequiredSeparation(
        "implementer", "contract_compliance_auditor", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_producer_not_sole_reviewer",
        "compliance is judged against work the implementer did",
    ),
    RequiredSeparation(
        "implementer", "release_verifier", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.2_producer_not_sole_reviewer",
        "the release verifier is the last check on the implementer's output",
    ),
    # -- §12.4 "an independent cross-family critic ... a separate adjudicator
    #    resolves each dispute". Separate from *both* prior steps, or step 3
    #    collapses into step 2.
    RequiredSeparation(
        "adversarial_critic", "judge", Dimension.AGENT, Strength.REQUIRED,
        "contract_12.4_produce_critique_adjudicate",
        "an adjudicator that is also the critic decides its own objection",
    ),
    # -- DEC-006: the mint refuses a holdout set whose kill rate against its
    #    declared mutants is below 1.0. If one author wrote both sides, the
    #    kill rate measures nothing.
    RequiredSeparation(
        "sealed_holdout_author", "mutant_author", Dimension.AGENT, Strength.REQUIRED,
        "DEC-006_mutation_gate_validates_the_holdout_set",
        "mutants are the only check on holdout strength; one author for both "
        "makes the kill rate self-certifying",
    ),
    # -- §17.4 "independent second-checker comparison where feasible".
    RequiredSeparation(
        "oracle_author", "release_verifier", Dimension.AGENT, Strength.CONDITIONAL,
        "contract_17.4_independent_second_checker",
        "the second checker on a minted oracle should not be its author",
    ),
    # -- §12.5 blind convergence / §12.2 cross-family where bias is material.
    RequiredSeparation(
        "researcher", "research_challenger", Dimension.FAMILY, Strength.CONDITIONAL,
        "contract_12.5_blind_convergence",
        "agreement between two members of one family is one opinion",
    ),
    RequiredSeparation(
        "planner", "plan_challenger", Dimension.FAMILY, Strength.CONDITIONAL,
        "contract_12.5_blind_convergence",
        "same, for the planning fork",
    ),
)


@dataclass(frozen=True)
class EdgeStatus:
    """One required edge, checked against both the pack's rules and the map."""

    required: RequiredSeparation
    declared: bool
    declared_advisory: bool
    holds_on_the_current_map: bool | None
    left_value: str | None
    right_value: str | None

    @property
    def mechanized(self) -> bool:
        """Declared *and* binding. An advisory rule is documentation."""
        return self.declared and not self.declared_advisory

    @property
    def unmechanized_requirement(self) -> bool:
        """A REQUIRED contract clause with no binding rule behind it (§13.4)."""
        return self.required.strength is Strength.REQUIRED and not self.mechanized

    def as_row(self) -> dict[str, Any]:
        return {
            "left": self.required.left,
            "right": self.required.right,
            "dimension": self.required.dimension.value,
            "strength": self.required.strength.value,
            "contract_ref": self.required.contract_ref,
            "declared_in_pack": self.declared,
            "declared_advisory_only": self.declared_advisory,
            "mechanized": self.mechanized,
            "holds_on_current_map": self.holds_on_the_current_map,
            "left_value": self.left_value,
            "right_value": self.right_value,
            "rationale": self.required.rationale,
        }


def _declared_index(policy: ModelPolicy) -> dict[frozenset[str], tuple[bool, bool, bool]]:
    """``pair -> (covers_agent, covers_family, advisory)`` from the pack's rules."""
    index: dict[frozenset[str], tuple[bool, bool, bool]] = {}
    for rule in policy.incompatibilities:
        if len(rule.roles) != 2:
            continue
        key = frozenset(rule.roles)
        agent, family, advisory = index.get(key, (False, False, True))
        index[key] = (
            agent or rule.requires_distinct_alias,
            family or rule.requires_distinct_family,
            advisory and rule.is_advisory,
        )
    return index


def evaluate(policy: ModelPolicy | None = None) -> list[EdgeStatus]:
    """Check every contract-required edge against the pack and the alias map."""
    policy = policy or load_model_policy()
    declared = _declared_index(policy)
    rows: list[EdgeStatus] = []

    for req in REQUIRED_SEPARATIONS:
        covers_agent, covers_family, advisory = declared.get(req.pair, (False, False, False))
        # A family rule implies distinct agents; a family rule therefore covers
        # an agent requirement, but not the reverse.
        is_declared = covers_family if req.dimension is Dimension.FAMILY else (
            covers_agent or covers_family
        )

        left_row = policy.roles.get(req.left)
        right_row = policy.roles.get(req.right)
        if left_row is None or right_row is None:
            rows.append(
                EdgeStatus(req, is_declared, is_declared and advisory, None, None, None)
            )
            continue

        if req.dimension is Dimension.FAMILY:
            left_value, right_value = left_row.family, right_row.family
        else:
            left_value, right_value = left_row.alias, right_row.alias

        rows.append(
            EdgeStatus(
                required=req,
                declared=is_declared,
                declared_advisory=is_declared and advisory,
                holds_on_the_current_map=left_value != right_value,
                left_value=left_value,
                right_value=right_value,
            )
        )
    return rows


def coverage_report(policy: ModelPolicy | None = None) -> dict[str, Any]:
    """A mechanical statement of where contract §12 is and is not enforced."""
    policy = policy or load_model_policy()
    rows = evaluate(policy)

    unmechanized = [r for r in rows if r.unmechanized_requirement]
    violated = [
        r
        for r in rows
        if r.holds_on_the_current_map is False and r.required.strength is Strength.REQUIRED
    ]
    named_in_a_rule = {name for rule in policy.incompatibilities for name in rule.roles}
    unconstrained_roles = sorted(set(policy.roles) - named_in_a_rule)

    binding_rules = [r for r in policy.incompatibilities if not r.is_advisory]
    star_centre_only = all("implementer" in r.roles for r in binding_rules) if binding_rules else False

    # Which families carry more than one gate-bearing assurance role. This is
    # the concentration FINDING-005 made concrete; reported as a label fact,
    # because family is a label and the transport is not guaranteed to match it.
    assurance = [
        name
        for name in policy.roles
        if name not in {"researcher", "research_challenger", "planner", "plan_challenger", "implementer"}
    ]
    by_family: dict[str, list[str]] = {}
    for name in assurance:
        row = policy.roles.get(name)
        if row is not None:
            by_family.setdefault(row.family, []).append(name)

    return {
        "check": "role_separation_coverage",
        "authority": "contract_sections_12.2_12.4_12.5_14.3_17.4_and_DEC-006",
        "oracle_type": "static_ast_type_policy",
        "family_separation_confidence": "label_verified",
        "family_separation_caveat": (
            "family is a label in model-policy.yaml; FINDING-005 measured three "
            "differently-labelled anthropic models resolving to one resold account "
            "pool, so a cross-family edge may not hold at the transport"
        ),
        "required_edges": len(rows),
        "mechanized_edges": sum(1 for r in rows if r.mechanized),
        "unmechanized_required_edges": [r.as_row() for r in unmechanized],
        "violated_on_current_map": [r.as_row() for r in violated],
        "binding_rules_all_centred_on_implementer": star_centre_only,
        "roles_named_in_no_rule": unconstrained_roles,
        "assurance_roles_by_family": {f: sorted(v) for f, v in sorted(by_family.items()) if len(v) > 1},
        "edges": [r.as_row() for r in rows],
    }
