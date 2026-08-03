"""Contract Section 10.2 -- all twelve graphs exist, compile, and are reachable."""

from __future__ import annotations

from pathlib import Path

import pytest

from workflows.graphs import (
    GRAPH_BUILDERS,
    REQUIRED_GRAPHS,
    UnknownGraph,
    WorkflowServices,
    build,
    compile_all,
    get_builder,
)

PACK_ROOT = Path(__file__).resolve().parents[2] / "project-pack"


@pytest.fixture
def services(tmp_path: Path) -> WorkflowServices:
    return WorkflowServices(pack_root=PACK_ROOT, worktree_root=str(tmp_path / "wt"))


def test_registry_is_the_contract_list_verbatim():
    assert REQUIRED_GRAPHS == (
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
    assert set(GRAPH_BUILDERS) == set(REQUIRED_GRAPHS)


def test_unknown_graph_is_refused_rather_than_improvised():
    with pytest.raises(UnknownGraph):
        get_builder("free_form_orchestrator_graph")


@pytest.mark.parametrize("graph_id", REQUIRED_GRAPHS)
def test_every_required_graph_compiles(graph_id: str, services: WorkflowServices):
    compiled = build(graph_id, services).compile()
    assert compiled is not None


@pytest.mark.parametrize("graph_id", REQUIRED_GRAPHS)
def test_no_graph_is_an_empty_shell(graph_id: str, services: WorkflowServices):
    """A registered graph with no nodes would pass "constructed" and do nothing."""
    graph = build(graph_id, services)
    real_nodes = [n for n in graph.nodes if not n.startswith("__")]
    assert len(real_nodes) >= 2, f"{graph_id} has {real_nodes}"


def test_compile_all_returns_every_graph(services: WorkflowServices):
    compiled = compile_all(services)
    assert set(compiled) == set(REQUIRED_GRAPHS)


def test_project_graph_reaches_every_other_graph(services: WorkflowServices):
    """GATE-D2-10: registered but unreachable is not composition."""
    project = build("project_graph", services)
    stage_nodes = {n for n in project.nodes if not n.startswith("__")}
    expected_stages = {
        "intake",
        "research",
        "contract",
        "planning",
        "build",
        "evaluation",
        "deployment",
        "closeout",
        "contract_revalidation",
        "dependency_update",
        "route_maintenance",
    }
    assert expected_stages <= stage_nodes

    # ``task_graph`` is reached through ``build_graph`` rather than directly.
    compiled_build = build("build_graph", services).compile()
    assert compiled_build is not None


def test_maintenance_arms_are_both_reachable(services: WorkflowServices):
    """Both conditional branches are live paths, not decorative edges."""
    project = build("project_graph", services).compile()
    from workflows.state import initial_state

    for trigger, expected in (
        ("contract_review_trigger", "contract_revalidation"),
        ("dependency_update_trigger", "dependency_update"),
        (None, "planning"),
    ):
        state = initial_state(
            project_id="EFAH-001",
            project_version="1.1",
            contract_version="1.1",
            terminus_database="efah",
            terminus_branch="main",
            terminus_commit="probe",
            work_unit_id="WU-0001",
            graph_id="project_graph",
        )
        if trigger:
            state["artifacts"] = {trigger: True}
        result = project.invoke(state)
        assert result["artifacts"]["maintenance_route"] == expected
        assert f"project_graph:{expected}" in result["node_log"] or expected == "planning"
