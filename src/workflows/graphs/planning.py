"""``planning_graph`` -- Contract Sections 9.4, 9.6, 10.2.

Compiles the frozen contract into Section 9.4 work units and validates the
dependency ordering before any of them is assigned. The validation is the point:
the ``project_compilation`` phase gate fails on "unlinked work or circular
role/dependency", so the graph checks both rather than trusting the compiler it
just ran.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from governance.envelope import content_hash
from governance.states import DriftFinding
from planning.decomposition import decompose, plan_hash
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "planning_graph"


def build_planning_graph(services: WorkflowServices) -> StateGraph:
    """START -> compile_work_units -> validate_linkage -> order_by_dependency -> END."""
    builder = StateGraph(WorkflowState)

    @node(GRAPH_ID, "compile_work_units", services)
    def compile_work_units(state: WorkflowState) -> dict[str, Any]:
        units = decompose(services.pack)
        return {
            "work_units": [u.model_dump(mode="json") for u in units],
            "output_hashes": {"planning.plan": plan_hash(units)},
            "artifacts": {"work_unit_count": len(units)},
        }

    @node(GRAPH_ID, "validate_linkage", services)
    def validate_linkage(state: WorkflowState) -> dict[str, Any]:
        """Section 19.2 ``UNLINKED_TASK``: every work unit traces to a requirement."""
        unlinked = [u["work_unit_id"] for u in state.get("work_units", []) if not u.get("requirement_ids")]
        ungated = [u["work_unit_id"] for u in state.get("work_units", []) if not u.get("gate_ids")]
        return {
            "typed_blockers": [str(DriftFinding.UNLINKED_TASK)] if unlinked else [],
            "artifacts": {"unlinked_work_units": unlinked, "ungated_work_units": ungated},
        }

    @node(GRAPH_ID, "order_by_dependency", services)
    def order_by_dependency(state: WorkflowState) -> dict[str, Any]:
        """Section 9.6: blocking gates come first; the order is deterministic.

        Sorting by ``(not blocking, work_unit_id)`` keeps the sequence stable
        across runs, which is what lets a resumed run pick up the same work unit
        it left rather than a differently-shuffled one.
        """
        units = list(state.get("work_units", []))
        gates = services.pack.acceptance_gates()

        def sort_key(unit: dict[str, Any]) -> tuple[int, int, str]:
            unit_gates = [gates[g] for g in unit.get("gate_ids", []) if g in gates]
            day = min((int(g.get("day", 9)) for g in unit_gates), default=9)
            blocking = 0 if any(g.get("blocking") for g in unit_gates) else 1
            return (day, blocking, unit["work_unit_id"])

        ordered = sorted(units, key=sort_key)
        return {
            "work_units": ordered,
            "output_hashes": {"planning.order": content_hash([u["work_unit_id"] for u in ordered])},
        }

    builder.add_node("compile_work_units", compile_work_units)
    builder.add_node("validate_linkage", validate_linkage)
    builder.add_node("order_by_dependency", order_by_dependency)
    builder.add_edge(START, "compile_work_units")
    builder.add_edge("compile_work_units", "validate_linkage")
    builder.add_edge("validate_linkage", "order_by_dependency")
    builder.add_edge("order_by_dependency", END)
    return builder
