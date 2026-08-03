"""Graph registry -- Contract Section 10.2's twelve required graphs.

The list is closed and checked at import time. A graph named in the contract but
not registered here raises on module import, so "we'll add that one later"
cannot survive a single test run. Registration is by construction, not by
declaration: each entry is a builder that returns a real ``StateGraph``, and
:func:`compile_all` proves every one of them compiles.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import StateGraph

from workflows.graphs._common import TerminusBinding, WorkflowServices, node
from workflows.graphs.assurance import (
    build_closeout_graph,
    build_deployment_graph,
    build_evaluation_graph,
)
from workflows.graphs.build import build_build_graph, build_task_graph
from workflows.graphs.contract import build_contract_graph, build_contract_revalidation_graph
from workflows.graphs.dependencies import build_dependency_update_graph
from workflows.graphs.intake import build_intake_graph, build_research_graph
from workflows.graphs.planning import build_planning_graph
from workflows.graphs.project import build_project_graph

GraphBuilder = Callable[[WorkflowServices], StateGraph]

#: Contract Section 10.2, verbatim and in contract order.
REQUIRED_GRAPHS: tuple[str, ...] = (
    "project_graph",
    "intake_graph",
    "research_graph",
    "contract_graph",
    "planning_graph",
    "build_graph",
    "task_graph",
    "evaluation_graph",
    "deployment_graph",
    "closeout_graph",
    "contract_revalidation_graph",
    "dependency_update_graph",
)

GRAPH_BUILDERS: dict[str, GraphBuilder] = {
    "project_graph": build_project_graph,
    "intake_graph": build_intake_graph,
    "research_graph": build_research_graph,
    "contract_graph": build_contract_graph,
    "planning_graph": build_planning_graph,
    "build_graph": build_build_graph,
    "task_graph": build_task_graph,
    "evaluation_graph": build_evaluation_graph,
    "deployment_graph": build_deployment_graph,
    "closeout_graph": build_closeout_graph,
    "contract_revalidation_graph": build_contract_revalidation_graph,
    "dependency_update_graph": build_dependency_update_graph,
}


class UnknownGraph(KeyError):
    """Asked for a graph Section 10.2 does not define."""


def _check_registry() -> None:
    missing = [name for name in REQUIRED_GRAPHS if name not in GRAPH_BUILDERS]
    extra = [name for name in GRAPH_BUILDERS if name not in REQUIRED_GRAPHS]
    if missing or extra:
        raise RuntimeError(
            f"graph registry disagrees with contract Section 10.2: missing={missing}, unexpected={extra}"
        )


_check_registry()


def get_builder(graph_id: str) -> GraphBuilder:
    if graph_id not in GRAPH_BUILDERS:
        raise UnknownGraph(f"{graph_id!r} is not one of the Section 10.2 graphs: {REQUIRED_GRAPHS}")
    return GRAPH_BUILDERS[graph_id]


def build(graph_id: str, services: WorkflowServices) -> StateGraph:
    return get_builder(graph_id)(services)


def compile_all(services: WorkflowServices, **compile_kwargs: Any) -> dict[str, Any]:
    """Compile every required graph. Reachability evidence for GATE-D2-10."""
    return {name: build(name, services).compile(**compile_kwargs) for name in REQUIRED_GRAPHS}


__all__ = [
    "GRAPH_BUILDERS",
    "REQUIRED_GRAPHS",
    "TerminusBinding",
    "UnknownGraph",
    "WorkflowServices",
    "build",
    "compile_all",
    "get_builder",
    "node",
]
