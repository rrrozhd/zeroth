"""Converts graph edges into runtime condition bindings.

A "binding" is a snapshot of an edge and its condition that the evaluator
can work with at runtime. Think of it like copying the relevant info from
the graph blueprint into a format the condition engine understands.
"""

from __future__ import annotations

from collections.abc import Iterable

from zeroth.conditions.models import ConditionBinding
from zeroth.graph.models import Edge, Graph


class ConditionBinder:
    """Creates runtime bindings from graph edges.

    Use this when you need to convert a graph (or a set of edges) into
    ConditionBinding objects that the evaluator and branch resolver can process.
    """

    def bind_graph(self, graph: Graph) -> list[ConditionBinding]:
        """Create bindings for every edge in the given graph."""
        return [self.bind_edge(graph.graph_id, edge) for edge in graph.edges]

    def bind_edges(self, graph_id: str, edges: Iterable[Edge]) -> list[ConditionBinding]:
        """Create bindings for a specific set of edges within a graph."""
        return [self.bind_edge(graph_id, edge) for edge in edges]

    def bind_edge(self, graph_id: str, edge: Edge) -> ConditionBinding:
        """Convert a single edge into a ConditionBinding ready for evaluation."""
        return ConditionBinding(
            graph_id=graph_id,
            edge_id=edge.edge_id,
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            condition=edge.condition,
            enabled=edge.enabled,
            # Copy metadata to a new dict so changes don't affect the original edge
            metadata=dict(edge.metadata),
        )
