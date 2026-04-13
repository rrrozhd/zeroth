"""Subgraph resolution and node ID namespacing.

Provides the ``SubgraphResolver`` that looks up a published graph by
its deployment reference, and helper functions for namespacing node IDs
and merging governance policy bindings from parent to child graphs.
"""

from __future__ import annotations

from dataclasses import dataclass

from zeroth.core.deployments.models import Deployment
from zeroth.core.deployments.service import DeploymentService
from zeroth.core.graph.models import Graph
from zeroth.core.graph.serialization import deserialize_graph
from zeroth.core.subgraph.errors import SubgraphResolutionError


@dataclass(slots=True)
class SubgraphResolver:
    """Resolves a subgraph graph_ref to a Graph via DeploymentService.

    Uses the existing ``DeploymentService.get()`` API to look up the
    deployment and deserializes the stored graph snapshot.
    """

    deployment_service: DeploymentService

    async def resolve(self, graph_ref: str, version: int | None = None) -> tuple[Graph, Deployment]:
        """Look up a deployment and return its deserialized graph.

        Parameters
        ----------
        graph_ref:
            The deployment reference name to look up.
        version:
            Optional specific deployment version.  ``None`` means latest.

        Returns:
        -------
        tuple[Graph, Deployment]:
            The deserialized graph and the deployment record.

        Raises:
        ------
        SubgraphResolutionError:
            If the deployment is not found or the stored graph cannot
            be deserialized.
        """
        deployment = await self.deployment_service.get(graph_ref, version)
        if deployment is None:
            version_part = f"version {version} " if version else ""
            msg = f"subgraph reference '{graph_ref}' {version_part}not found"
            raise SubgraphResolutionError(msg)

        try:
            graph = deserialize_graph(deployment.serialized_graph)
        except Exception as exc:
            msg = f"subgraph reference '{graph_ref}' deserialization failed: {exc}"
            raise SubgraphResolutionError(msg) from exc

        return graph, deployment


def namespace_subgraph(graph: Graph, graph_ref: str, depth: int) -> Graph:
    """Prefix all node and edge IDs to prevent collisions across nesting levels.

    Returns a new Graph with all identifiers prefixed with
    ``subgraph:{graph_ref}:{depth}:``.  The original graph is never modified.

    Parameters
    ----------
    graph:
        The child graph whose IDs need namespacing.
    graph_ref:
        The deployment reference used as part of the prefix.
    depth:
        The nesting depth (0-based) used as part of the prefix.

    Returns:
    -------
    Graph:
        A copy of the graph with all IDs prefixed.
    """
    prefix = f"subgraph:{graph_ref}:{depth}:"

    namespaced_nodes = [
        node.model_copy(update={"node_id": f"{prefix}{node.node_id}"}) for node in graph.nodes
    ]

    namespaced_edges = [
        edge.model_copy(
            update={
                "edge_id": f"{prefix}{edge.edge_id}",
                "source_node_id": f"{prefix}{edge.source_node_id}",
                "target_node_id": f"{prefix}{edge.target_node_id}",
            }
        )
        for edge in graph.edges
    ]

    namespaced_entry = f"{prefix}{graph.entry_step}" if graph.entry_step else None

    return graph.model_copy(
        update={
            "nodes": namespaced_nodes,
            "edges": namespaced_edges,
            "entry_step": namespaced_entry,
        }
    )


def merge_governance(parent_graph: Graph, subgraph: Graph) -> Graph:
    """Prepend parent policy bindings to the subgraph's bindings.

    This leverages PolicyGuard's existing intersection semantics to
    enforce parent-ceiling governance: the subgraph can only restrict,
    never relax, the parent's capabilities.

    Returns a new Graph; the original subgraph is never modified.

    Parameters
    ----------
    parent_graph:
        The parent graph whose policies take precedence.
    subgraph:
        The child graph to merge policies into.

    Returns:
    -------
    Graph:
        A copy of the subgraph with merged policy bindings.
    """
    merged_policy_bindings = list(parent_graph.policy_bindings) + list(subgraph.policy_bindings)
    return subgraph.model_copy(update={"policy_bindings": merged_policy_bindings})
