"""Cycle detection and the safeguard rule that makes a cycle publishable."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.core.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.core.graph.models import Edge, Graph, Node


def validate_cycles(
    graph: Graph,
    node_map: dict[str, Node],
    adjacency: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> None:
    """Detect unsafe cycles in the graph.

    Cycles are allowed only when the graph has a configured safeguard that
    prevents infinite execution.
    """
    components = strongly_connected_components(node_map.keys(), adjacency)
    for component in components:
        if len(component) == 1:
            node_id = next(iter(component))
            if node_id not in adjacency.get(node_id, []):
                continue

        component_edges = [
            edge
            for edge in graph.edges
            if edge.enabled
            and edge.kind != "tool"
            and edge.source_node_id in component
            and edge.target_node_id in component
        ]
        if component_has_safeguard(graph, component_edges):
            continue

        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.UNSAFE_CYCLE,
            message="cyclic graph path must declare a safeguard",
            graph_id=graph.graph_id,
            details={
                "nodes": sorted(component),
                "edges": [edge.edge_id for edge in component_edges],
            },
        )


def component_has_safeguard(graph: Graph, edges: list[Edge]) -> bool:
    """Return True if a cycle has something preventing infinite loops."""
    if graph.execution_settings.max_visits_per_edge is not None:
        return True
    return any(edge.condition and edge.condition.allow_cycle_traversal for edge in edges)


def strongly_connected_components(
    node_ids: Iterable[str],
    adjacency: dict[str, list[str]],
) -> list[set[str]]:
    """Find all groups of nodes that can reach each other (Tarjan's algorithm).

    Each group returned is a set of node IDs that form a cycle.
    Single nodes without self-loops are also returned but filtered later.
    """
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[set[str]] = []

    def strongconnect(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)

        for neighbour in adjacency.get(node_id, []):
            if neighbour not in indices:
                strongconnect(neighbour)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[neighbour])
            elif neighbour in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[neighbour])

        if lowlinks[node_id] == indices[node_id]:
            component: set[str] = set()
            while stack:
                current = stack.pop()
                on_stack.remove(current)
                component.add(current)
                if current == node_id:
                    break
            components.append(component)

    for node_id in node_ids:
        if node_id not in indices:
            strongconnect(node_id)

    return components
