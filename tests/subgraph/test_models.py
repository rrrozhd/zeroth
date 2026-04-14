"""Tests for subgraph models, errors, and Node/Run type extensions."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from zeroth.core.graph.models import Graph, Node
from zeroth.core.graph.serialization import deserialize_graph, serialize_graph
from zeroth.core.runs.models import Run
from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.core.subgraph.models import SubgraphNodeData


# ---------------------------------------------------------------------------
# SubgraphNodeData
# ---------------------------------------------------------------------------


class TestSubgraphNodeData:
    """Tests for SubgraphNodeData model."""

    def test_accepts_required_fields(self) -> None:
        data = SubgraphNodeData(graph_ref="my-workflow")
        assert data.graph_ref == "my-workflow"
        assert data.version is None
        assert data.thread_participation == "inherit"
        assert data.max_depth == 3

    def test_accepts_all_fields(self) -> None:
        data = SubgraphNodeData(
            graph_ref="other-workflow",
            version=2,
            thread_participation="isolated",
            max_depth=5,
        )
        assert data.graph_ref == "other-workflow"
        assert data.version == 2
        assert data.thread_participation == "isolated"
        assert data.max_depth == 5

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            SubgraphNodeData(graph_ref="x", unknown_field="bad")

    def test_max_depth_ge_1(self) -> None:
        with pytest.raises(ValidationError):
            SubgraphNodeData(graph_ref="x", max_depth=0)

    def test_max_depth_le_10(self) -> None:
        with pytest.raises(ValidationError):
            SubgraphNodeData(graph_ref="x", max_depth=11)

    def test_version_none_default(self) -> None:
        data = SubgraphNodeData(graph_ref="x")
        assert data.version is None

    def test_version_accepts_int(self) -> None:
        data = SubgraphNodeData(graph_ref="x", version=1)
        assert data.version == 1


# ---------------------------------------------------------------------------
# SubgraphNode (lives in graph/models.py)
# ---------------------------------------------------------------------------


class TestSubgraphNode:
    """Tests for SubgraphNode in the Node union."""

    def test_subgraph_node_creation(self) -> None:
        from zeroth.core.graph.models import SubgraphNode

        node = SubgraphNode(
            node_id="sub-1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-workflow"),
        )
        assert node.node_type == "subgraph"
        assert node.subgraph.graph_ref == "child-workflow"

    def test_subgraph_node_extends_node_base(self) -> None:
        from zeroth.core.graph.models import NodeBase, SubgraphNode

        assert issubclass(SubgraphNode, NodeBase)

    def test_to_governed_step_spec(self) -> None:
        from zeroth.core.graph.models import SubgraphNode

        node = SubgraphNode(
            node_id="sub-1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf", version=2),
        )
        spec = node.to_governed_step_spec()
        assert spec.name == "sub-1"
        assert spec.agent["kind"] == "subgraph_ref"
        assert spec.agent["graph_ref"] == "child-wf"
        assert spec.agent["version"] == 2

    def test_node_union_accepts_subgraph(self) -> None:
        """SubgraphNode is part of the discriminated Node union."""
        from zeroth.core.graph.models import SubgraphNode

        # Validate via the Node discriminated union
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Node)
        raw = {
            "node_type": "subgraph",
            "node_id": "sub-1",
            "graph_version_ref": "g1@1",
            "subgraph": {"graph_ref": "child-wf"},
        }
        node = adapter.validate_python(raw)
        assert isinstance(node, SubgraphNode)

    def test_subgraph_node_accepts_parallel_config(self) -> None:
        """Phase 43 (D-05, D-23): SubgraphNode now accepts parallel_config.

        The prior Phase 41 ``_reject_parallel_config`` model validator was
        removed unconditionally — SubgraphNode inside a parallel fan-out
        branch is supported composition.
        """
        from zeroth.core.graph.models import SubgraphNode
        from zeroth.core.parallel.models import ParallelConfig

        node = SubgraphNode(
            node_id="sub-1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf"),
            parallel_config=ParallelConfig(split_path="items"),
        )
        assert node.parallel_config is not None
        assert node.parallel_config.split_path == "items"


# ---------------------------------------------------------------------------
# Graph with SubgraphNode round-trip
# ---------------------------------------------------------------------------


class TestGraphSubgraphRoundTrip:
    """Tests for serializing/deserializing graphs containing SubgraphNode."""

    def test_graph_with_subgraph_node_roundtrip(self) -> None:
        from zeroth.core.graph.models import SubgraphNode

        node = SubgraphNode(
            node_id="sub-1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf", version=3),
        )
        graph = Graph(
            graph_id="g1",
            name="parent-workflow",
            entry_step="sub-1",
            nodes=[node],
        )
        serialized = serialize_graph(graph)
        restored = deserialize_graph(serialized)
        assert len(restored.nodes) == 1
        assert restored.nodes[0].node_type == "subgraph"
        assert restored.nodes[0].subgraph.graph_ref == "child-wf"
        assert restored.nodes[0].subgraph.version == 3

    def test_existing_agent_node_still_deserializes(self) -> None:
        """Ensure backward compatibility with AgentNode graphs."""
        from zeroth.core.graph.models import AgentNode, AgentNodeData

        node = AgentNode(
            node_id="a1",
            graph_version_ref="g1@1",
            agent=AgentNodeData(
                instruction="do things",
                model_provider="openai/gpt-4",
            ),
        )
        graph = Graph(
            graph_id="g1",
            name="agent-workflow",
            entry_step="a1",
            nodes=[node],
        )
        serialized = serialize_graph(graph)
        restored = deserialize_graph(serialized)
        assert restored.nodes[0].node_type == "agent"

    def test_existing_eu_node_still_deserializes(self) -> None:
        """Ensure backward compatibility with ExecutableUnitNode graphs."""
        from zeroth.core.graph.models import ExecutableUnitNode, ExecutableUnitNodeData

        node = ExecutableUnitNode(
            node_id="eu1",
            graph_version_ref="g1@1",
            executable_unit=ExecutableUnitNodeData(
                manifest_ref="manifest-1",
                execution_mode="native",
            ),
        )
        graph = Graph(
            graph_id="g1",
            name="eu-workflow",
            entry_step="eu1",
            nodes=[node],
        )
        serialized = serialize_graph(graph)
        restored = deserialize_graph(serialized)
        assert restored.nodes[0].node_type == "executable_unit"

    def test_existing_human_approval_node_still_deserializes(self) -> None:
        """Ensure backward compatibility with HumanApprovalNode graphs."""
        from zeroth.core.graph.models import HumanApprovalNode, HumanApprovalNodeData

        node = HumanApprovalNode(
            node_id="ha1",
            graph_version_ref="g1@1",
            human_approval=HumanApprovalNodeData(),
        )
        graph = Graph(
            graph_id="g1",
            name="approval-workflow",
            entry_step="ha1",
            nodes=[node],
        )
        serialized = serialize_graph(graph)
        restored = deserialize_graph(serialized)
        assert restored.nodes[0].node_type == "human_approval"


# ---------------------------------------------------------------------------
# Run model parent_run_id
# ---------------------------------------------------------------------------


class TestRunParentRunId:
    """Tests for the parent_run_id field on Run."""

    def test_run_default_parent_run_id_none(self) -> None:
        run = Run(
            graph_version_ref="g1@1",
            deployment_ref="dep-1",
        )
        assert run.parent_run_id is None

    def test_run_accepts_parent_run_id(self) -> None:
        run = Run(
            graph_version_ref="g1@1",
            deployment_ref="dep-1",
            parent_run_id="parent-abc123",
        )
        assert run.parent_run_id == "parent-abc123"

    def test_existing_run_creation_backward_compat(self) -> None:
        """Existing code that creates Runs without parent_run_id still works."""
        run = Run(
            graph_version_ref="g1@1",
            deployment_ref="dep-1",
            tenant_id="tenant-1",
            workspace_id="ws-1",
        )
        assert run.tenant_id == "tenant-1"
        assert run.workspace_id == "ws-1"
        assert run.parent_run_id is None


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestSubgraphErrors:
    """Tests for the subgraph error classes."""

    def test_subgraph_error_is_runtime_error(self) -> None:
        assert issubclass(SubgraphError, RuntimeError)

    def test_subgraph_depth_limit_error_is_subgraph_error(self) -> None:
        assert issubclass(SubgraphDepthLimitError, SubgraphError)
        assert issubclass(SubgraphDepthLimitError, RuntimeError)

    def test_subgraph_resolution_error_is_subgraph_error(self) -> None:
        assert issubclass(SubgraphResolutionError, SubgraphError)
        assert issubclass(SubgraphResolutionError, RuntimeError)

    def test_subgraph_execution_error_is_subgraph_error(self) -> None:
        assert issubclass(SubgraphExecutionError, SubgraphError)
        assert issubclass(SubgraphExecutionError, RuntimeError)

    def test_subgraph_cycle_error_is_subgraph_error(self) -> None:
        assert issubclass(SubgraphCycleError, SubgraphError)
        assert issubclass(SubgraphCycleError, RuntimeError)

    def test_errors_are_raisable_with_message(self) -> None:
        with pytest.raises(SubgraphDepthLimitError, match="max depth 3"):
            raise SubgraphDepthLimitError("max depth 3 exceeded")

        with pytest.raises(SubgraphResolutionError, match="not found"):
            raise SubgraphResolutionError("graph ref not found")

        with pytest.raises(SubgraphExecutionError, match="child failed"):
            raise SubgraphExecutionError("child failed")

        with pytest.raises(SubgraphCycleError, match="cycle detected"):
            raise SubgraphCycleError("cycle detected")
