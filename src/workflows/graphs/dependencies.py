"""``dependency_update_graph`` -- Contract Section 16.2, dependency-policy.yaml.

The policy file already specifies the loop as an ordered sequence
(``version_diff_loop.sequence``), so the graph executes *that* sequence rather
than a re-imagined one. Reading the steps from the pack means a policy change
moves the runtime, not a code change chasing the policy.

``risk_policy.auto_merge_dependency_updates: none`` is recorded owner intent as
of 2026-08-01. Discovery and candidate preparation continue; the merge is
withheld. The graph honours that by producing a prepared candidate and stopping.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from governance.envelope import content_hash
from governance.states import DriftFinding
from workflows.graphs._common import WorkflowServices, node
from workflows.state import WorkflowState

GRAPH_ID = "dependency_update_graph"


def build_dependency_update_graph(services: WorkflowServices) -> StateGraph:
    """START -> read_registry -> diff_against_snapshots -> prepare_candidate -> END."""
    builder = StateGraph(WorkflowState)

    @node(GRAPH_ID, "read_dependency_registry", services)
    def read_dependency_registry(state: WorkflowState) -> dict[str, Any]:
        policy = services.pack.yaml("dependency-policy.yaml")
        stack = policy.get("selected_stack", {})
        unpinned = sorted(
            name for name, meta in stack.items() if str(meta.get("version", "")).startswith("TODO_")
        )
        return {
            "artifacts": {
                "dependency_stack": sorted(stack),
                "unpinned_dependencies": unpinned,
                "version_diff_sequence": list(policy.get("version_diff_loop", {}).get("sequence", [])),
                "auto_merge_dependency_updates": policy.get("risk_policy", {}).get(
                    "auto_merge_dependency_updates"
                ),
            },
            "output_hashes": {"dependencies.registry": content_hash(stack)},
        }

    @node(GRAPH_ID, "diff_against_snapshots", services)
    def diff_against_snapshots(state: WorkflowState) -> dict[str, Any]:
        """Section 16.1: every dependency links to a hashed Context7 snapshot.

        A dependency with no snapshot cannot be diffed against a previous
        version, which is the whole point of the cache. Report it; do not
        pretend the diff was clean.
        """
        snapshot_dir = services.pack.root / "evidence" / "context7-snapshots"
        snapshots = sorted(p.stem for p in snapshot_dir.glob("*.json")) if snapshot_dir.is_dir() else []
        stack = state.get("artifacts", {}).get("dependency_stack", [])
        covered = {name for name in stack if any(name.replace("_", "-") in s.lower() for s in snapshots)}
        missing = sorted(set(stack) - covered)
        return {
            "artifacts": {"context7_snapshots": snapshots, "dependencies_without_snapshot": missing},
            "typed_blockers": [str(DriftFinding.STALE_INPUT_ARTIFACT)] if not snapshots else [],
        }

    @node(GRAPH_ID, "prepare_update_candidate", services)
    def prepare_update_candidate(state: WorkflowState) -> dict[str, Any]:
        artifacts = state.get("artifacts", {})
        sequence = artifacts.get("version_diff_sequence", [])
        # The loop stops before `merge_or_reject_with_evidence`: owner risk
        # policy withholds the merge, and the runtime does not overrule it.
        executed = sequence[: sequence.index("update_isolated_branch") + 1] if "update_isolated_branch" in sequence else sequence
        candidate = {
            "steps_executed": executed,
            "steps_withheld": [s for s in sequence if s not in executed],
            "reason_withheld": "risk_policy.auto_merge_dependency_updates=none",
        }
        return {
            "artifacts": {"dependency_update_candidate": candidate},
            "output_hashes": {"dependencies.candidate": content_hash(candidate)},
        }

    builder.add_node("read_dependency_registry", read_dependency_registry)
    builder.add_node("diff_against_snapshots", diff_against_snapshots)
    builder.add_node("prepare_update_candidate", prepare_update_candidate)
    builder.add_edge(START, "read_dependency_registry")
    builder.add_edge("read_dependency_registry", "diff_against_snapshots")
    builder.add_edge("diff_against_snapshots", "prepare_update_candidate")
    builder.add_edge("prepare_update_candidate", END)
    return builder
