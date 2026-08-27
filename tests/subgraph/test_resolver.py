"""Tests for SubgraphResolver, namespace_subgraph, and merge_governance."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    Graph,
    IfNode,
    IfNodeData,
)
from zeroth.contracts.graph.validation.control_nodes import canonical_if_route_condition
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.runtime.subgraphs.errors import SubgraphResolutionError
from zeroth.runtime.subgraphs.resolver import (
    SubgraphResolver,
    merge_governance,
    namespace_subgraph,
)
from zeroth.runtime.subgraphs import resolver as resolver_module
from zeroth.service.deployments.models import Deployment

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

        resolver.deployment_service.get.assert_awaited_once_with(
            "child-ref", None, tenant_id=None, workspace_id=None
        )
        assert result_graph.graph_id == "child-g"
        assert result_deployment.deployment_id == "dep-123"

    @pytest.mark.asyncio
    async def test_resolve_with_version_passes_version(self) -> None:
        graph = _make_graph()
        deployment = _make_deployment(graph)
        resolver = _make_resolver(deployment)

        await resolver.resolve("child-ref", version=2)

        resolver.deployment_service.get.assert_awaited_once_with(
            "child-ref", 2, tenant_id=None, workspace_id=None
        )

    @pytest.mark.asyncio
    async def test_resolve_forwards_tenant_scope(self) -> None:
        # Audit S7: resolve() must forward the parent run's tenant/workspace so
        # the deployment lookup is tenant-scoped (deployment_ref is global).
        graph = _make_graph()
        resolver = _make_resolver(_make_deployment(graph))

        await resolver.resolve("child-ref", version=3, tenant_id="tenant-a", workspace_id="ws-a")

        resolver.deployment_service.get.assert_awaited_once_with(
            "child-ref", 3, tenant_id="tenant-a", workspace_id="ws-a"
        )

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

    @pytest.mark.asyncio
    async def test_resolve_refuses_foreign_tenant_ref(self, sqlite_db) -> None:
        """S7 (real DB): a deployment_ref owned by tenant B must NOT resolve for a.

        run owned by tenant A — cross-tenant subgraph execution is fail-closed.
        """
        from zeroth.contracts.graph import GraphRepository
        from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository

        graph = _make_graph()
        repo = SQLiteDeploymentRepository(sqlite_db)
        owned_by_b = Deployment(
            deployment_id="dep-b",
            deployment_ref="shared-ref",
            version=1,
            graph_id=graph.graph_id,
            graph_version=graph.version,
            graph_version_ref=f"{graph.graph_id}@{graph.version}",
            serialized_graph=serialize_graph(graph),
            tenant_id="tenant-b",
            workspace_id=None,
        )
        await repo.create(owned_by_b, tenant_id="tenant-b", workspace_id=None)
        resolver = SubgraphResolver(
            deployment_service=DeploymentService(
                graph_repository=GraphRepository(sqlite_db),
                deployment_repository=repo,
            )
        )

        # The owning tenant resolves its own ref.
        _graph, deployment = await resolver.resolve("shared-ref", tenant_id="tenant-b")
        assert deployment.tenant_id == "tenant-b"

        # A foreign tenant is refused (fail closed) — not silently handed B's graph.
        with pytest.raises(SubgraphResolutionError, match="not found"):
            await resolver.resolve("shared-ref", tenant_id="tenant-a")

        # Unscoped compatibility is bound to the reserved default tenant; it
        # cannot act as cross-tenant authority for a named tenant's deployment.
        with pytest.raises(SubgraphResolutionError, match="not found"):
            await resolver.resolve("shared-ref")


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

    @pytest.mark.parametrize("route", ["true", "false"])
    @pytest.mark.parametrize("branch_index", [None, 7])
    def test_recanonicalizes_if_route_condition_for_namespace(
        self,
        route: str,
        branch_index: int | None,
    ) -> None:
        decision = IfNode(
            node_id="pause-decision",
            graph_version_ref="child-g@1",
            condition=IfNodeData(expression="payload.index == 7"),
        )
        target = AgentNode(
            node_id="approval-delay",
            graph_version_ref="child-g@1",
            agent=AgentNodeData(instruction="delay", model_provider="openai/gpt-4"),
        )
        original_condition = canonical_if_route_condition("pause-decision", route)
        graph = Graph(
            graph_id="child-g",
            name="child-g",
            version=1,
            nodes=[decision, target],
            edges=[
                Edge(
                    edge_id="decision-approval",
                    source_node_id="pause-decision",
                    target_node_id="approval-delay",
                    condition=original_condition,
                    metadata={"source_handle": route},
                )
            ],
            entry_step="pause-decision",
        )

        namespaced = namespace_subgraph(
            graph,
            "approval-child",
            depth=1,
            branch_index=branch_index,
        )

        branch_prefix = "" if branch_index is None else f"branch:{branch_index}:"
        qualified_id = f"{branch_prefix}subgraph:approval-child:1:pause-decision"
        assert namespaced.edges[0].condition == canonical_if_route_condition(
            qualified_id,
            route,
        )
        assert graph.edges[0].condition == original_condition

    def test_preserves_non_if_custom_condition(self) -> None:
        graph = _make_graph()
        custom = Condition(expression="payload.route == 'custom'", metadata={"owner": "author"})
        graph.edges[0] = graph.edges[0].model_copy(update={"condition": custom})

        namespaced = namespace_subgraph(graph, "child-ref", depth=1, branch_index=3)

        assert namespaced.edges[0].condition == custom
        assert graph.edges[0].condition == custom

    def test_preserves_if_tool_edge_condition(self) -> None:
        decision = IfNode(
            node_id="pause-decision",
            graph_version_ref="child-g@1",
            condition=IfNodeData(expression="payload.index == 7"),
        )
        target = AgentNode(
            node_id="tool-target",
            graph_version_ref="child-g@1",
            agent=AgentNodeData(instruction="tool", model_provider="openai/gpt-4"),
        )
        custom = Condition(expression="payload.tool_enabled == True")
        graph = Graph(
            graph_id="child-g",
            name="child-g",
            version=1,
            nodes=[decision, target],
            edges=[
                Edge(
                    edge_id="tool-edge",
                    source_node_id="pause-decision",
                    target_node_id="tool-target",
                    kind="tool",
                    condition=custom,
                    metadata={"source_handle": "true"},
                )
            ],
            entry_step="pause-decision",
        )

        namespaced = namespace_subgraph(graph, "child-ref", depth=1, branch_index=3)

        assert namespaced.edges[0].condition == custom
        assert graph.edges[0].condition == custom

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("index", "route"), [(7, "true"), (6, "false")])
    async def test_namespaced_if_dispatch_selects_one_qualified_target(
        self,
        index: int,
        route: str,
    ) -> None:
        from zeroth.contracts.conditions import BranchResolver
        from zeroth.contracts.conditions.models import ConditionContext
        from zeroth.runtime.orchestration import NodeDispatcher, RuntimeToolExecutor
        from zeroth.runtime.runs import Run

        decision = IfNode(
            node_id="pause-decision",
            graph_version_ref="child-g@1",
            condition=IfNodeData(expression="payload.index == 7"),
        )
        targets = [
            AgentNode(
                node_id=f"{candidate}-target",
                graph_version_ref="child-g@1",
                agent=AgentNodeData(
                    instruction=candidate,
                    model_provider="openai/gpt-4",
                ),
            )
            for candidate in ("true", "false")
        ]
        graph = Graph(
            graph_id="child-g",
            name="child-g",
            version=1,
            nodes=[decision, *targets],
            edges=[
                Edge(
                    edge_id=f"{candidate}-edge",
                    source_node_id="pause-decision",
                    target_node_id=f"{candidate}-target",
                    condition=canonical_if_route_condition("pause-decision", candidate),
                    metadata={"source_handle": candidate},
                )
                for candidate in ("true", "false")
            ],
            entry_step="pause-decision",
        )
        namespaced = namespace_subgraph(graph, "approval-child", depth=1, branch_index=7)
        qualified_source = "branch:7:subgraph:approval-child:1:pause-decision"
        namespaced_decision = next(
            node for node in namespaced.nodes if node.node_id == qualified_source
        )
        unused_runner = object()
        dispatcher = NodeDispatcher(
            agent_runners={},
            executable_unit_runner=unused_runner,
            tool_executor=RuntimeToolExecutor(executable_unit_runner=unused_runner),
        )

        output, _audit = await dispatcher.dispatch_inner(
            namespaced_decision,
            Run(graph_version_ref="parent@1", deployment_ref="parent"),
            {"index": index},
        )
        resolution = BranchResolver().resolve(
            namespaced,
            qualified_source,
            ConditionContext(payload=output),
        )

        assert resolution.next_node_ids == [
            f"branch:7:subgraph:approval-child:1:{route}-target"
        ]
        assert len(resolution.active_edge_ids) == 1
        assert len(resolution.suppressed_edge_ids) == 1

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

    def test_runner_key_strips_only_parallel_branch_prefixes(self) -> None:
        canonical_runner_id = getattr(resolver_module, "canonical_runner_id", None)

        assert canonical_runner_id is not None
        assert (
            canonical_runner_id("branch:7:subgraph:child-ref:1:a1")
            == "subgraph:child-ref:1:a1"
        )
        assert canonical_runner_id("subgraph:child-ref:1:a1") == "subgraph:child-ref:1:a1"


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
