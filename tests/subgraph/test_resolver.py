"""Tests for SubgraphResolver, namespace_subgraph, and merge_governance."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.core.deployments.models import Deployment
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    Graph,
    SubgraphNode,
)
from zeroth.core.graph.serialization import serialize_graph
from zeroth.core.subgraph.errors import SubgraphResolutionError
from zeroth.core.subgraph.models import SubgraphNodeData
from zeroth.core.subgraph.resolver import (
    SubgraphResolver,
    merge_governance,
    namespace_subgraph,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(
    graph_id: str = "child-g",
    name: str = "child-workflow",
    entry_step: str | None = "a1",
    policy_bindings: list[str] | None = None,
) -> Graph:
    """Create a simple graph for testing."""
    node = AgentNode(
        node_id="a1",
        graph_version_ref=f"{graph_id}@1",
        agent=AgentNodeData(
            instruction="do something",
            model_provider="openai/gpt-4",
        ),
    )
    node2 = AgentNode(
        node_id="a2",
        graph_version_ref=f"{graph_id}@1",
        agent=AgentNodeData(
            instruction="do more",
            model_provider="openai/gpt-4",
        ),
    )
    edge = Edge(
        edge_id="e1",
        source_node_id="a1",
        target_node_id="a2",
    )
    return Graph(
        graph_id=graph_id,
        name=name,
        entry_step=entry_step,
        nodes=[node, node2],
        edges=[edge],
        policy_bindings=policy_bindings or [],
    )


def _make_deployment(graph: Graph) -> Deployment:
    """Create a mock deployment with a serialized graph."""
    return Deployment(
        deployment_id="dep-123",
        deployment_ref="child-ref",
        version=1,
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}@{graph.version}",
        serialized_graph=serialize_graph(graph),
    )


def _make_resolver(deployment: Deployment | None = None) -> SubgraphResolver:
    """Create a SubgraphResolver with a mocked deployment service."""
    svc = AsyncMock()
    svc.get = AsyncMock(return_value=deployment)
    return SubgraphResolver(deployment_service=svc)


# ---------------------------------------------------------------------------
# SubgraphResolver.resolve()
# ---------------------------------------------------------------------------


class TestSubgraphResolverResolve:
    """Tests for SubgraphResolver.resolve()."""

    @pytest.mark.asyncio
    async def test_resolve_calls_deployment_service_get_with_ref_and_none(self) -> None:
        graph = _make_graph()
        deployment = _make_deployment(graph)
        resolver = _make_resolver(deployment)

        result_graph, result_deployment = await resolver.resolve("child-ref")

        resolver.deployment_service.get.assert_awaited_once_with("child-ref", None)
        assert result_graph.graph_id == "child-g"
        assert result_deployment.deployment_id == "dep-123"

    @pytest.mark.asyncio
    async def test_resolve_with_version_passes_version(self) -> None:
        graph = _make_graph()
        deployment = _make_deployment(graph)
        resolver = _make_resolver(deployment)

        await resolver.resolve("child-ref", version=2)

        resolver.deployment_service.get.assert_awaited_once_with("child-ref", 2)

    @pytest.mark.asyncio
    async def test_resolve_raises_resolution_error_when_not_found(self) -> None:
        resolver = _make_resolver(None)

        with pytest.raises(SubgraphResolutionError, match="not found"):
            await resolver.resolve("missing-ref")

    @pytest.mark.asyncio
    async def test_resolve_raises_resolution_error_with_version_in_message(self) -> None:
        resolver = _make_resolver(None)

        with pytest.raises(SubgraphResolutionError, match="version 5"):
            await resolver.resolve("missing-ref", version=5)

    @pytest.mark.asyncio
    async def test_resolve_wraps_deserialization_errors(self) -> None:
        """Malformed serialized_graph should be wrapped in SubgraphResolutionError."""
        bad_deployment = Deployment(
            deployment_id="dep-bad",
            deployment_ref="bad-ref",
            version=1,
            graph_id="bad-g",
            graph_version=1,
            graph_version_ref="bad-g@1",
            serialized_graph="not-valid-json{{{",
        )
        resolver = _make_resolver(bad_deployment)

        with pytest.raises(SubgraphResolutionError, match="deserialization"):
            await resolver.resolve("bad-ref")


# ---------------------------------------------------------------------------
# namespace_subgraph()
# ---------------------------------------------------------------------------


class TestNamespaceSubgraph:
    """Tests for the namespace_subgraph function."""

    def test_prefixes_node_ids(self) -> None:
        graph = _make_graph()
        ns = namespace_subgraph(graph, "child-ref", depth=1)

        node_ids = [n.node_id for n in ns.nodes]
        assert node_ids == [
            "subgraph:child-ref:1:a1",
            "subgraph:child-ref:1:a2",
        ]

    def test_prefixes_edge_fields(self) -> None:
        graph = _make_graph()
        ns = namespace_subgraph(graph, "child-ref", depth=1)

        edge = ns.edges[0]
        assert edge.edge_id == "subgraph:child-ref:1:e1"
        assert edge.source_node_id == "subgraph:child-ref:1:a1"
        assert edge.target_node_id == "subgraph:child-ref:1:a2"

    def test_prefixes_entry_step(self) -> None:
        graph = _make_graph(entry_step="a1")
        ns = namespace_subgraph(graph, "child-ref", depth=1)

        assert ns.entry_step == "subgraph:child-ref:1:a1"

    def test_returns_copy_original_unchanged(self) -> None:
        graph = _make_graph()
        original_node_ids = [n.node_id for n in graph.nodes]
        original_edge_ids = [e.edge_id for e in graph.edges]

        namespace_subgraph(graph, "child-ref", depth=1)

        # Original graph is NOT modified
        assert [n.node_id for n in graph.nodes] == original_node_ids
        assert [e.edge_id for e in graph.edges] == original_edge_ids

    def test_depth_0_produces_correct_prefix(self) -> None:
        graph = _make_graph()
        ns = namespace_subgraph(graph, "my-ref", depth=0)

        assert ns.nodes[0].node_id == "subgraph:my-ref:0:a1"

    def test_none_entry_step_remains_none(self) -> None:
        graph = _make_graph(entry_step=None)
        # Remove the entry_step validation issue by clearing nodes/edges
        graph = Graph(graph_id="g1", name="empty")
        ns = namespace_subgraph(graph, "ref", depth=1)
        assert ns.entry_step is None


# ---------------------------------------------------------------------------
# merge_governance()
# ---------------------------------------------------------------------------


class TestMergeGovernance:
    """Tests for the merge_governance function."""

    def test_prepends_parent_policy_bindings(self) -> None:
        parent = _make_graph(policy_bindings=["parent-policy-1", "parent-policy-2"])
        subgraph = _make_graph(policy_bindings=["child-policy-1"])

        merged = merge_governance(parent, subgraph)

        assert merged.policy_bindings == [
            "parent-policy-1",
            "parent-policy-2",
            "child-policy-1",
        ]

    def test_returns_copy_original_unchanged(self) -> None:
        parent = _make_graph(policy_bindings=["parent-policy"])
        subgraph = _make_graph(policy_bindings=["child-policy"])

        merged = merge_governance(parent, subgraph)

        # Original subgraph is NOT modified
        assert subgraph.policy_bindings == ["child-policy"]
        # Merged is different
        assert merged.policy_bindings == ["parent-policy", "child-policy"]

    def test_empty_parent_policies_leaves_subgraph_unchanged(self) -> None:
        parent = _make_graph(policy_bindings=[])
        subgraph = _make_graph(policy_bindings=["child-policy"])

        merged = merge_governance(parent, subgraph)

        assert merged.policy_bindings == ["child-policy"]
