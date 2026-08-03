"""``project_graph`` -- the top-level composition (Contract Section 10.2).

This is the walking skeleton's spine: intake -> research -> contract ->
(maintenance) -> planning -> build -> evaluation -> deployment -> closeout, with
``contract_revalidation_graph`` and ``dependency_update_graph`` reachable through
a real conditional edge rather than existing beside the graph unreferenced.

Subgraphs are invoked from inside a node instead of being added as graph nodes.
The reason is the ``node_log`` reducer: a subgraph that shares an ``operator.add``
channel with its parent writes the *whole* accumulated list back through the
parent reducer, duplicating every prior entry. Invoking explicitly lets the
parent contribute the delta, which is what a durable log of "what actually ran"
requires.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "project_graph"

#: State keys the parent recomputes itself; a subgraph's copy is not merged back.
_PARENT_OWNED = ("node_log", "graph_node", "graph_id")


def _invoke_subgraph(compiled: Any, state: WorkflowState) -> dict[str, Any]:
    """Run a subgraph and return the parent-facing delta.

    ``node_log`` is returned as the *new* entries only. Every other channel is
    returned whole: the reducers for hashes, gates and blockers are idempotent
    merges, so re-writing an unchanged value is a no-op.
    """
    before = list(state.get("node_log", []))
    result = dict(compiled.invoke(dict(state)))
    after = list(result.get("node_log", []))
    delta = after[len(before) :] if after[: len(before)] == before else after
    out = {k: v for k, v in result.items() if k not in _PARENT_OWNED}
    out["node_log"] = delta
    return out


def build_project_graph(services: WorkflowServices) -> StateGraph:
    """Compose the eleven other graphs into one durable project run."""
    from workflows.graphs.assurance import (
        build_closeout_graph,
        build_deployment_graph,
        build_evaluation_graph,
    )
    from workflows.graphs.build import build_build_graph
    from workflows.graphs.contract import build_contract_graph, build_contract_revalidation_graph
    from workflows.graphs.dependencies import build_dependency_update_graph
    from workflows.graphs.intake import build_intake_graph, build_research_graph
    from workflows.graphs.planning import build_planning_graph

    stages: dict[str, Callable[[WorkflowServices], StateGraph]] = {
        "intake": build_intake_graph,
        "research": build_research_graph,
        "contract": build_contract_graph,
        "planning": build_planning_graph,
        "build": build_build_graph,
        "evaluation": build_evaluation_graph,
        "deployment": build_deployment_graph,
        "closeout": build_closeout_graph,
        "contract_revalidation": build_contract_revalidation_graph,
        "dependency_update": build_dependency_update_graph,
    }
    compiled = {name: factory(services).compile() for name, factory in stages.items()}

    builder = StateGraph(WorkflowState)

    def stage_node(name: str) -> Any:
        @node(GRAPH_ID, name, services)
        def run_stage(state: WorkflowState) -> dict[str, Any]:
            return _invoke_subgraph(compiled[name], state)

        return run_stage

    for name in stages:
        builder.add_node(name, stage_node(name))

    @node(GRAPH_ID, "route_maintenance", services)
    def route_maintenance(state: WorkflowState) -> dict[str, Any]:
        """Section 19.4: the review is periodic *and* event-triggered.

        The trigger is state, not a coin flip, so a test can drive either arm
        and both arms are genuinely reachable.
        """
        artifacts = state.get("artifacts", {})
        if artifacts.get("contract_review_trigger"):
            route = "contract_revalidation"
        elif artifacts.get("dependency_update_trigger"):
            route = "dependency_update"
        else:
            route = "planning"
        return {"artifacts": {"maintenance_route": route}}

    builder.add_node("route_maintenance", route_maintenance)

    def choose_route(state: WorkflowState) -> str:
        return state.get("artifacts", {}).get("maintenance_route", "planning")

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "research")
    builder.add_edge("research", "contract")
    builder.add_edge("contract", "route_maintenance")
    builder.add_conditional_edges(
        "route_maintenance",
        choose_route,
        {
            "contract_revalidation": "contract_revalidation",
            "dependency_update": "dependency_update",
            "planning": "planning",
        },
    )
    builder.add_edge("contract_revalidation", "planning")
    builder.add_edge("dependency_update", "planning")
    builder.add_edge("planning", "build")
    builder.add_edge("build", "evaluation")
    builder.add_edge("evaluation", "deployment")
    builder.add_edge("deployment", "closeout")
    builder.add_edge("closeout", END)
    return builder
