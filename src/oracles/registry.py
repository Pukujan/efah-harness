"""Oracle registry and hierarchy routing.

Contract Section 17.3 is binding, not advisory: *an available higher-level
deterministic oracle MUST NOT be replaced by a lower-level subjective one.*
:func:`route` enforces that mechanically, and :class:`RoutingDecision` records
which candidates were available so the choice is auditable after the fact --
GATE-D2-20 A6 ``oracle_route_audit``.

The practical failure this prevents is small and easy: a builder cannot get a
deterministic check green, reaches for the judge, the judge says "looks
correct", and the gate goes green on an opinion. Routing down the hierarchy has
to be impossible, not merely discouraged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oracles.base import DETERMINISTIC_LEVELS, DeterministicOracle, OracleNotMinted
from oracles.definitions import ORACLE_IDS, load_all_definitions, load_minted
from oracles.oracle_001_composition import CompositionReachabilityOracle
from oracles.oracle_002_lease_fencing import LeaseFencingOracle
from oracles.oracle_003_provenance import ProvenanceBindingOracle

IMPLEMENTATIONS: dict[str, type[DeterministicOracle]] = {
    "ORACLE-001": CompositionReachabilityOracle,
    "ORACLE-002": LeaseFencingOracle,
    "ORACLE-003": ProvenanceBindingOracle,
}

#: The dotted module implementing each oracle's verdict path, for the
#: structural no-judge proof (Section 17.4).
VERDICT_PATH_MODULES: dict[str, str] = {
    "ORACLE-001": "oracles.oracle_001_composition",
    "ORACLE-002": "oracles.oracle_002_lease_fencing",
    "ORACLE-003": "oracles.oracle_003_provenance",
}


class HierarchyViolation(RuntimeError):
    """Raised when a lower-level oracle is chosen while a higher one is available."""


@dataclass
class RoutingDecision:
    question: str
    selected_oracle_id: str | None
    selected_level: int | None
    considered: list[tuple[str, int]] = field(default_factory=list)
    rejected_lower_levels: list[tuple[str, int]] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "oracle_route_audit",
            "expected": "route_selects_highest_available_level",
            "question": self.question,
            "selected": self.selected_oracle_id,
            "selected_hierarchy_level": self.selected_level,
            "candidates": [{"oracle_id": o, "level": lvl} for o, lvl in self.considered],
            "not_selected_because_a_higher_level_was_available": [
                {"oracle_id": o, "level": lvl} for o, lvl in self.rejected_lower_levels
            ],
        }


def build_oracles(minted_dir: Any = None) -> dict[str, DeterministicOracle]:
    """Construct every oracle from its pack definition and minted record."""
    definitions = load_all_definitions()
    built: dict[str, DeterministicOracle] = {}
    for oracle_id in ORACLE_IDS:
        implementation = IMPLEMENTATIONS[oracle_id]
        built[oracle_id] = implementation(
            definitions[oracle_id], minted=load_minted(oracle_id, minted_dir)
        )
    return built


def route(
    question: str, candidates: list[DeterministicOracle], *, allow_subjective: bool = False
) -> RoutingDecision:
    """Select the highest-level (numerically lowest) available oracle.

    ``allow_subjective`` never permits a level 6/7 route while a deterministic
    candidate is present. It only governs whether a subjective oracle may be
    used when nothing deterministic is available at all.
    """
    ranked = sorted(candidates, key=lambda o: o.hierarchy_level)
    considered = [(o.oracle_id, o.hierarchy_level) for o in ranked]
    if not ranked:
        return RoutingDecision(question, None, None, considered)

    deterministic = [o for o in ranked if o.hierarchy_level in DETERMINISTIC_LEVELS]
    if deterministic:
        chosen = deterministic[0]
    elif allow_subjective:
        chosen = ranked[0]
    else:
        raise HierarchyViolation(
            f"{question}: no deterministic oracle available and subjective routing is not allowed"
        )

    rejected = [
        (o.oracle_id, o.hierarchy_level) for o in ranked if o.hierarchy_level > chosen.hierarchy_level
    ]
    return RoutingDecision(question, chosen.oracle_id, chosen.hierarchy_level, considered, rejected)


def assert_no_downgrade(decision: RoutingDecision, candidates: list[DeterministicOracle]) -> None:
    """GATE-D2-20 A6. Raise if a higher-level oracle was available and skipped."""
    if decision.selected_level is None:
        return
    higher = [
        o.oracle_id
        for o in candidates
        if o.hierarchy_level < decision.selected_level
    ]
    if higher:
        raise HierarchyViolation(
            f"{decision.question}: selected level {decision.selected_level} while "
            f"higher-level oracles were available: {higher}"
        )


def require_minted(oracle: DeterministicOracle) -> None:
    record = getattr(oracle, "_minted", {}) or {}
    if not record.get("minted"):
        raise OracleNotMinted(
            f"{oracle.oracle_id} has no minted record; Section 17.4 forbids gating on it"
        )
