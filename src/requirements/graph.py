"""Requirement / task / dependency graph.

Contract Section 9.6 defines the coverage and the edge vocabulary; Section 8.1
makes the graph a compilation gate: "Graph is acyclic where required, all tasks
link to requirements. Unlinked work, missing gate, or circular role assignment
fails compilation."

GATE-D1-03 is the acceptance gate for this module:

* A2 zero unlinked tasks  -> :meth:`DependencyGraph.unlinked_tasks`
* A3 zero cycles in the task and role graphs -> :meth:`DependencyGraph.cycles`
* A4 a non-empty critical path -> :meth:`DependencyGraph.critical_path`

Direction convention, and it matters for cycle detection: ``A depends_on B``
means A needs B first, so the edge points from dependent to prerequisite.
``blocks`` is the same relation read the other way and is recorded for Section
9.6 completeness; the two are never mixed inside one cycle scan, because a
correctly emitted pair (``A depends_on B``, ``B blocks A``) would otherwise look
like a two-node cycle.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from governance.states import DriftFinding

#: Contract Section 9.6. The closed edge vocabulary.
EDGE_TYPES: frozenset[str] = frozenset(
    {
        "depends_on",
        "blocks",
        "supported_by",
        "derived_from",
        "implemented_by",
        "tested_by",
        "verified_by",
        "evaluated_by",
        "invalidated_by",
        "supersedes",
        "compatible_with",
        "conflicts_with",
        "produced_by",
        "deployed_to",
    }
)

#: Section 9.6 dependency-map coverage classes.
DEPENDENCY_CLASSES: tuple[str, ...] = (
    "task",
    "requirement",
    "artifact",
    "software_package",
    "service",
    "documentation",
    "evaluation_and_oracle",
    "deployment_environment",
    "knowledge_and_gold",
)


class GraphError(ValueError):
    """Structural violation: unknown edge type or dangling endpoint."""


@dataclass(frozen=True)
class Node:
    node_id: str
    kind: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    edge_type: str
    dependency_class: str
    rationale: str = ""

    @property
    def edge_id(self) -> str:
        return f"DEP-{self.edge_type}:{self.source}->{self.target}"

    def as_body(self) -> dict[str, Any]:
        return {
            "dependency_id": self.edge_id,
            "source": self.source,
            "target": self.target,
            "edge_type": self.edge_type,
            "dependency_class": self.dependency_class,
            "rationale": self.rationale,
        }


@dataclass
class CycleReport:
    """Evidence for GATE-D1-03 A3."""

    scanned: dict[str, int] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)

    @property
    def acyclic(self) -> bool:
        return not self.cycles

    def as_body(self) -> dict[str, Any]:
        return {
            "scanned_subgraphs": self.scanned,
            "cycles": self.cycles,
            "cycle_count": len(self.cycles),
            "acyclic_where_required": self.acyclic,
        }


@dataclass
class CriticalPath:
    """Evidence for GATE-D1-03 A4."""

    nodes: list[str] = field(default_factory=list)
    length: int = 0
    weight: float = 0.0

    def as_body(self) -> dict[str, Any]:
        return {
            "path": self.nodes,
            "length": self.length,
            "weight": self.weight,
            "method": "longest_weighted_path_over_depends_on_edges_in_topological_order",
        }


class DependencyGraph:
    """Typed multi-relation graph over the compiled control-plane objects."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._seen: set[tuple[str, str, str]] = set()

    # -- construction ------------------------------------------------------

    def add_node(self, node_id: str, kind: str, **attributes: Any) -> Node:
        if node_id in self._nodes:
            existing = self._nodes[node_id]
            if existing.kind != kind:
                raise GraphError(f"node {node_id!r} redeclared as {kind!r} (was {existing.kind!r})")
            existing.attributes.update(attributes)
            return existing
        node = Node(node_id=node_id, kind=kind, attributes=dict(attributes))
        self._nodes[node_id] = node
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        *,
        dependency_class: str,
        rationale: str = "",
    ) -> Edge:
        if edge_type not in EDGE_TYPES:
            raise GraphError(f"unknown edge type {edge_type!r}; Section 9.6 vocabulary is closed")
        if dependency_class not in DEPENDENCY_CLASSES:
            raise GraphError(f"unknown dependency class {dependency_class!r}")
        for endpoint in (source, target):
            if endpoint not in self._nodes:
                raise GraphError(f"edge {edge_type} references unknown node {endpoint!r}")
        key = (source, target, edge_type)
        if key in self._seen:
            return next(e for e in self._edges if (e.source, e.target, e.edge_type) == key)
        edge = Edge(
            source=source,
            target=target,
            edge_type=edge_type,
            dependency_class=dependency_class,
            rationale=rationale,
        )
        self._edges.append(edge)
        self._seen.add(key)
        return edge

    # -- access ------------------------------------------------------------

    @property
    def nodes(self) -> dict[str, Node]:
        return self._nodes

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges)

    def nodes_of_kind(self, kind: str) -> list[str]:
        return sorted(n for n, node in self._nodes.items() if node.kind == kind)

    def edges_of_type(self, edge_type: str) -> list[Edge]:
        return [e for e in self._edges if e.edge_type == edge_type]

    def edge_type_counts(self) -> dict[str, int]:
        counts = {t: 0 for t in sorted(EDGE_TYPES)}
        for edge in self._edges:
            counts[edge.edge_type] += 1
        return counts

    def adjacency(self, edge_types: Iterable[str], node_kinds: Iterable[str] | None = None) -> dict[str, list[str]]:
        wanted = set(edge_types)
        kinds = set(node_kinds) if node_kinds is not None else None
        adjacency: dict[str, list[str]] = defaultdict(list)
        for node_id, node in self._nodes.items():
            if kinds is None or node.kind in kinds:
                adjacency.setdefault(node_id, [])
        for edge in self._edges:
            if edge.edge_type not in wanted:
                continue
            if kinds is not None:
                if self._nodes[edge.source].kind not in kinds or self._nodes[edge.target].kind not in kinds:
                    continue
            adjacency[edge.source].append(edge.target)
        return {k: sorted(v) for k, v in adjacency.items()}

    # -- GATE-D1-03 A2: every task links to at least one requirement -------

    def unlinked_tasks(self) -> list[str]:
        """Task nodes with no incoming ``implemented_by`` edge from a Requirement.

        Section 8.1: unlinked work fails compilation. The finding type is
        :attr:`governance.states.DriftFinding.UNLINKED_TASK`.
        """
        linked: set[str] = set()
        for edge in self._edges:
            if edge.edge_type != "implemented_by":
                continue
            if self._nodes[edge.source].kind == "Requirement" and self._nodes[edge.target].kind == "Task":
                linked.add(edge.target)
        return [t for t in self.nodes_of_kind("Task") if t not in linked]

    def unlinked_task_findings(self) -> list[dict[str, Any]]:
        return [
            {
                "finding": str(DriftFinding.UNLINKED_TASK),
                "task_id": task_id,
                "detail": "task has no implemented_by edge from any Requirement",
            }
            for task_id in self.unlinked_tasks()
        ]

    # -- GATE-D1-03 A3: acyclic where required -----------------------------

    def cycles(self) -> CycleReport:
        """Scan every subgraph the contract requires to be acyclic.

        Task graph: ``depends_on`` and (separately) ``blocks`` over Task nodes.
        Role graph: ``verified_by`` over Role nodes -- a role that transitively
        validates its own producer is the circular validation Section 12.2
        forbids.
        """
        report = CycleReport()
        for label, edge_types, kinds in (
            ("task_depends_on", ("depends_on",), ("Task",)),
            ("task_blocks", ("blocks",), ("Task",)),
            ("role_verified_by", ("verified_by",), ("Role",)),
            ("requirement_derived_from", ("derived_from",), ("Requirement",)),
        ):
            adjacency = self.adjacency(edge_types, kinds)
            report.scanned[label] = sum(len(v) for v in adjacency.values())
            for cycle in _find_cycles(adjacency):
                report.cycles.append([label] + cycle)
        return report

    # -- GATE-D1-03 A4: critical path --------------------------------------

    def critical_path(self) -> CriticalPath:
        """Longest weighted chain of ``depends_on`` edges over Task nodes.

        Weight is ``estimate_units`` on the task node (default 1). Returned in
        execution order -- deepest prerequisite first -- because that is the
        order the schedule runs, not the order the recursion unwound.
        """
        adjacency = self.adjacency(("depends_on",), ("Task",))
        order = _topological_order(adjacency)
        if order is None:
            return CriticalPath()

        best_weight: dict[str, float] = {}
        successor: dict[str, str | None] = {}
        # ``order`` lists prerequisites before dependents, so every prerequisite
        # already has its best_weight when its dependent is processed.
        for node_id in order:
            weight = float(self._nodes[node_id].attributes.get("estimate_units", 1))
            best_child: str | None = None
            best_child_weight = 0.0
            for prerequisite in adjacency.get(node_id, []):
                candidate = best_weight.get(prerequisite, 0.0)
                if candidate > best_child_weight:
                    best_child_weight = candidate
                    best_child = prerequisite
            best_weight[node_id] = weight + best_child_weight
            successor[node_id] = best_child

        if not best_weight:
            return CriticalPath()
        head = max(sorted(best_weight), key=lambda n: best_weight[n])
        chain: list[str] = []
        cursor: str | None = head
        while cursor is not None:
            chain.append(cursor)
            cursor = successor.get(cursor)
        chain.reverse()  # prerequisites first
        return CriticalPath(nodes=chain, length=len(chain), weight=best_weight[head])

    # -- export ------------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Graph export with edge types -- GATE-D1-03 evidence_required item 2."""
        return {
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "nodes_by_kind": {
                kind: len(self.nodes_of_kind(kind)) for kind in sorted({n.kind for n in self._nodes.values()})
            },
            "edges_by_type": self.edge_type_counts(),
            "dependency_classes_covered": sorted({e.dependency_class for e in self._edges}),
            "nodes": [
                {"id": n.node_id, "kind": n.kind, **n.attributes} for n in sorted(self._nodes.values(), key=lambda n: n.node_id)
            ],
            "edges": [e.as_body() for e in self._edges],
        }


def _topological_order(adjacency: dict[str, list[str]]) -> list[str] | None:
    """Kahn's algorithm. Returns ``None`` when the subgraph has a cycle."""
    indegree: dict[str, int] = {n: 0 for n in adjacency}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] = indegree.get(target, 0) + 1
    queue = sorted(n for n, d in indegree.items() if d == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for target in adjacency.get(node, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    if len(order) != len(indegree):
        return None
    # Reverse so that a node appears after everything it depends on.
    order.reverse()
    return order


def _find_cycles(adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Depth-first colouring. Returns each distinct cycle once, as a node list."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in adjacency}
    stack: list[str] = []
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        colour[node] = GREY
        stack.append(node)
        for target in adjacency.get(node, []):
            if target not in colour:
                colour[target] = WHITE
            if colour[target] == WHITE:
                visit(target)
            elif colour[target] == GREY:
                cycle = stack[stack.index(target):] + [target]
                key = tuple(_canonical_rotation(cycle[:-1]))
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
        stack.pop()
        colour[node] = BLACK

    for node in sorted(adjacency):
        if colour.get(node, WHITE) == WHITE:
            visit(node)
    return found


def _canonical_rotation(cycle: list[str]) -> list[str]:
    if not cycle:
        return cycle
    pivot = cycle.index(min(cycle))
    return cycle[pivot:] + cycle[:pivot]
