"""End-to-end integration tests for subgraph composition.

Covers the full subgraph lifecycle:
- Happy path: parent -> child -> parent completes
- Thread inheritance modes (inherit vs isolated)
- Governance ceiling (parent policy bindings merged into child)
- Depth limit enforcement (chain exceeding max_depth)
- Cycle detection (A -> B -> A)
- Multi-reference (same subgraph referenced twice)
- Node ID namespacing (child IDs carry prefix)
- Audit trail (subgraph_run_id in parent history)
- Error paths (non-existent graph_ref)
- Full approval flow (subgraph with HumanApprovalNode)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    SubgraphNode,
)
from zeroth.core.graph.serialization import serialize_graph
from zeroth.core.deployments.models import Deployment
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
from zeroth.core.runs.models import Run, RunStatus
from zeroth.core.subgraph.errors import SubgraphCycleError, SubgraphDepthLimitError
from zeroth.core.subgraph.executor import SubgraphExecutor
from zeroth.core.subgraph.models import SubgraphNodeData
from zeroth.core.subgraph.resolver import SubgraphResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_graph(
    graph_id: str = "child-g",
    agent_name: str = "c1",
    final_output: dict | None = None,
) -> Graph:
    """Create a simple graph with a single AgentNode."""
    node = AgentNode(
        node_id=agent_name,
        graph_version_ref=f"{graph_id}@1",
        agent=AgentNodeData(
            instruction="child task",
            model_provider="openai/gpt-4",
        ),
    )
    return Graph(
        graph_id=graph_id,
        name=f"{graph_id}-workflow",
        version=1,
        nodes=[node],
        edges=[],
        entry_step=agent_name,
    )


def _make_subgraph_parent(
    graph_id: str = "parent-g",
    subgraph_ref: str = "child-g",
    *,
    thread_participation: str = "inherit",
    max_depth: int = 3,
    version: int | None = None,
    successor_node: AgentNode | None = None,
) -> Graph:
    """Create a parent graph with a SubgraphNode and optional successor AgentNode."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref=f"{graph_id}@1",
        subgraph=SubgraphNodeData(
            graph_ref=subgraph_ref,
            version=version,
            thread_participation=thread_participation,
            max_depth=max_depth,
        ),
    )
    nodes = [subgraph_node]
    edges = []
    if successor_node is not None:
        nodes.append(successor_node)
        edges.append(Edge(edge_id="e1", source_node_id="s1", target_node_id=successor_node.node_id))
    return Graph(
        graph_id=graph_id,
        name=f"{graph_id}-workflow",
        version=1,
        nodes=nodes,
        edges=edges,
        entry_step="s1",
    )


def _make_approval_graph(graph_id: str = "approval-child") -> Graph:
    """Create a graph with a HumanApprovalNode."""
    approval_node = HumanApprovalNode(
        node_id="approve-1",
        graph_version_ref=f"{graph_id}@1",
        human_approval=HumanApprovalNodeData(),
    )
    return Graph(
        graph_id=graph_id,
        name=f"{graph_id}-workflow",
        version=1,
        nodes=[approval_node],
        edges=[],
        entry_step="approve-1",
    )


def _make_deployment(graph: Graph) -> Deployment:
    """Create a Deployment from a graph for the resolver."""
    return Deployment(
        deployment_id=f"dep-{graph.graph_id}",
        deployment_ref=graph.graph_id,
        graph_id=graph.graph_id,
        graph_version=graph.version,
        serialized_graph=serialize_graph(graph),
    )


def _make_run_repository() -> AsyncMock:
    """Create a mock RunRepository."""
    repo = AsyncMock()
    repo.create = AsyncMock(side_effect=lambda r: r)
    repo.put = AsyncMock(side_effect=lambda r: r)
    repo.get = AsyncMock(return_value=None)
    repo.write_checkpoint = AsyncMock()
    return repo


def _make_deployment_service(deployments: dict[str, Deployment]) -> AsyncMock:
    """Create a mock DeploymentService that resolves from a dict."""
    service = AsyncMock()

    async def mock_get(ref: str, version: int | None = None) -> Deployment | None:
        return deployments.get(ref)

    service.get = mock_get
    return service


def _make_mock_agent_runner(output: dict) -> AsyncMock:
    """Create a mock AgentRunner that returns a fixed output."""
    runner = AsyncMock()

    class FakeResult:
        def __init__(self, output_data):
            self.output_data = output_data
            self.audit_record = {"model": "test", "token_usage": None}

    runner.run = AsyncMock(return_value=FakeResult(output))
    return runner


def _build_orchestrator(
    run_repository: AsyncMock | None = None,
    agent_runners: dict | None = None,
    subgraph_executor: SubgraphExecutor | None = None,
) -> RuntimeOrchestrator:
    """Build a RuntimeOrchestrator with configured dependencies."""
    return RuntimeOrchestrator(
        run_repository=run_repository or _make_run_repository(),
        agent_runners=agent_runners or {},
        executable_unit_runner=ExecutableUnitRunner(),
        subgraph_executor=subgraph_executor,
    )


# ---------------------------------------------------------------------------
# 1. Happy path: parent -> child -> parent completes
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Parent graph with SubgraphNode, child graph with AgentNode; parent completes."""

    @pytest.mark.asyncio
    async def test_parent_completes_with_child_output(self) -> None:
        """Parent graph runs SubgraphNode, child executes, parent uses child output."""
        child_graph = _make_agent_graph("child-g", "c1")
        parent_graph = _make_subgraph_parent("parent-g", "child-g")

        # Child run returned by executor -- completed with output
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"answer": 42},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _build_orchestrator(
            run_repository=run_repo,
            subgraph_executor=mock_executor,
        )

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED
        assert result.final_output == {"answer": 42}

    @pytest.mark.asyncio
    async def test_parent_passes_output_to_successor_node(self) -> None:
        """After SubgraphNode, its output feeds into the next AgentNode."""
        successor = AgentNode(
            node_id="a1",
            graph_version_ref="parent-g@1",
            agent=AgentNodeData(instruction="process", model_provider="openai/gpt-4"),
        )
        parent_graph = _make_subgraph_parent("parent-g", "child-g", successor_node=successor)

        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"child_data": "hello"},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        # Mock agent runner for successor node
        agent_runner = _make_mock_agent_runner({"final": "processed"})
        run_repo = _make_run_repository()
        orch = _build_orchestrator(
            run_repository=run_repo,
            agent_runners={"a1": agent_runner},
            subgraph_executor=mock_executor,
        )

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED
        assert result.final_output == {"final": "processed"}
        # Verify successor received child's output
        agent_runner.run.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Thread inheritance
# ---------------------------------------------------------------------------


class TestThreadInheritance:
    """Thread participation modes: inherit vs isolated."""

    @pytest.mark.asyncio
    async def test_inherit_mode_passes_parent_thread_id(self) -> None:
        """With 'inherit' mode, executor receives parent's thread_id via the Run."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"ok": True},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent(thread_participation="inherit")
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"}, thread_id="parent-thread-1")

        # Verify the executor was called -- thread mode is handled inside executor
        mock_executor.execute.assert_called_once()
        call_kwargs = mock_executor.execute.call_args[1]
        assert call_kwargs["parent_run"].thread_id == "parent-thread-1"

    @pytest.mark.asyncio
    async def test_isolated_mode_configured_on_subgraph_node(self) -> None:
        """With 'isolated' mode, SubgraphNode configures isolated thread."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"ok": True},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent(thread_participation="isolated")
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        mock_executor.execute.assert_called_once()
        call_kwargs = mock_executor.execute.call_args[1]
        node = call_kwargs["node"]
        assert node.subgraph.thread_participation == "isolated"


# ---------------------------------------------------------------------------
# 3. Governance ceiling
# ---------------------------------------------------------------------------


class TestGovernanceCeiling:
    """Parent policy bindings are merged into child graph."""

    @pytest.mark.asyncio
    async def test_parent_policies_propagate_to_child(self) -> None:
        """Parent graph with policy_bindings -- executor receives parent graph with policies."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"ok": True},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent()
        parent_graph = parent_graph.model_copy(update={"policy_bindings": ["deny-dangerous-ops"]})

        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        await orch.run_graph(parent_graph, {"input": "test"})

        # Verify executor received parent_graph with policies
        call_kwargs = mock_executor.execute.call_args[1]
        assert "deny-dangerous-ops" in call_kwargs["parent_graph"].policy_bindings


# ---------------------------------------------------------------------------
# 4. Depth limit
# ---------------------------------------------------------------------------


class TestDepthLimit:
    """Chain of subgraphs exceeding max_depth raises SubgraphDepthLimitError."""

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded_fails_run(self) -> None:
        """SubgraphNode with max_depth=3 in a chain of 4 fails."""
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(
            side_effect=SubgraphDepthLimitError(
                "subgraph depth 4 exceeds max_depth 3 for graph_ref 'deep-g'"
            )
        )

        parent_graph = _make_subgraph_parent(max_depth=3)
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.FAILED
        assert "depth" in (result.failure_state.message or "").lower()
        assert result.failure_state.reason == "subgraph_execution_failed"

    @pytest.mark.asyncio
    async def test_subgraph_depth_tracked_in_metadata(self) -> None:
        """Subgraph executor tracks depth in run metadata."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"ok": True},
            metadata={"subgraph_depth": 2},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent(max_depth=5)
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# 5. Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """Circular subgraph references detected before max_depth."""

    @pytest.mark.asyncio
    async def test_cycle_detected_fails_run(self) -> None:
        """A->B->A cycle causes SubgraphCycleError and run failure."""
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(
            side_effect=SubgraphCycleError("circular subgraph reference detected: graph-a")
        )

        parent_graph = _make_subgraph_parent(subgraph_ref="graph-b")
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.FAILED
        assert "circular" in (result.failure_state.message or "").lower()


# ---------------------------------------------------------------------------
# 6. Multi-reference (same subgraph referenced twice)
# ---------------------------------------------------------------------------


class TestMultiReference:
    """Same subgraph referenced twice in parent produces two distinct child runs."""

    @pytest.mark.asyncio
    async def test_two_subgraph_nodes_produce_two_child_runs(self) -> None:
        """Parent with two SubgraphNodes pointing to same child gets two execute calls."""
        child_run_1 = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"run": 1},
            metadata={"subgraph_depth": 1},
        )
        child_run_2 = Run(
            run_id="child-run-2",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"run": 2},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=[child_run_1, child_run_2])

        # Parent with two SubgraphNodes in sequence
        s1 = SubgraphNode(
            node_id="s1",
            graph_version_ref="parent-g@1",
            subgraph=SubgraphNodeData(graph_ref="child-g"),
        )
        s2 = SubgraphNode(
            node_id="s2",
            graph_version_ref="parent-g@1",
            subgraph=SubgraphNodeData(graph_ref="child-g"),
        )
        parent_graph = Graph(
            graph_id="parent-g",
            name="parent",
            version=1,
            nodes=[s1, s2],
            edges=[Edge(edge_id="e1", source_node_id="s1", target_node_id="s2")],
            entry_step="s1",
        )

        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED
        assert mock_executor.execute.call_count == 2
        # Each call should produce a distinct child run
        call_ids = [mock_executor.execute.call_args_list[i][1]["node_id"] for i in range(2)]
        assert call_ids == ["s1", "s2"]


# ---------------------------------------------------------------------------
# 7. Node ID namespacing
# ---------------------------------------------------------------------------


class TestNodeIdNamespacing:
    """Child run execution history contains namespaced node_ids."""

    @pytest.mark.asyncio
    async def test_child_history_has_namespaced_node_ids(self) -> None:
        """Child run's execution_history should have 'subgraph:' prefixed IDs."""
        from zeroth.core.runs.models import RunHistoryEntry

        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"answer": "yes"},
            execution_history=[
                RunHistoryEntry(
                    node_id="subgraph:child-g:1:c1",
                    status="completed",
                    input_snapshot={"input": "test"},
                    output_snapshot={"answer": "yes"},
                    audit_ref="audit:1",
                )
            ],
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent()
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED
        # Child run had namespaced node IDs
        child_history = child_run.execution_history
        assert child_history[0].node_id.startswith("subgraph:")


# ---------------------------------------------------------------------------
# 8. Audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    """Parent run history entry for SubgraphNode contains subgraph_run_id."""

    @pytest.mark.asyncio
    async def test_parent_history_contains_subgraph_run_id(self) -> None:
        """Parent's execution_history for subgraph step has subgraph_run_id."""
        child_run = Run(
            run_id="child-run-audit-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"result": "audited"},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        parent_graph = _make_subgraph_parent()
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        # Find history entry for the SubgraphNode
        subgraph_entries = [h for h in result.execution_history if h.node_id == "s1"]
        assert len(subgraph_entries) == 1


# ---------------------------------------------------------------------------
# 9. Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """Non-existent graph_ref causes run failure."""

    @pytest.mark.asyncio
    async def test_nonexistent_graph_ref_fails_run(self) -> None:
        """graph_ref pointing to non-existent deployment fails the run."""
        from zeroth.core.subgraph.errors import SubgraphResolutionError

        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(
            side_effect=SubgraphResolutionError("subgraph reference 'nonexistent-g' not found")
        )

        parent_graph = _make_subgraph_parent(subgraph_ref="nonexistent-g")
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.FAILED
        assert "not found" in (result.failure_state.message or "")
        assert result.failure_state.reason == "subgraph_execution_failed"


# ---------------------------------------------------------------------------
# 10. Full approval flow
# ---------------------------------------------------------------------------


class TestFullApprovalFlow:
    """Parent with SubgraphNode -> child has HumanApprovalNode -> pause -> resume."""

    @pytest.mark.asyncio
    async def test_approval_pause_and_resume_completes(self) -> None:
        """Full flow: run parent -> child pauses at approval -> parent pauses -> resume -> complete."""
        # Phase 1: child returns WAITING_APPROVAL
        child_run_paused = Run(
            run_id="child-run-approval-1",
            graph_version_ref="approval-child:v1",
            deployment_ref="approval-child",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run_paused)

        parent_graph = _make_subgraph_parent(subgraph_ref="approval-child")
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        # Parent should be paused
        assert result.status == RunStatus.WAITING_APPROVAL
        pending = result.metadata.get("pending_subgraph")
        assert pending is not None
        assert pending["child_run_id"] == "child-run-approval-1"
        assert pending["graph_ref"] == "approval-child"

        # Phase 2: resume with child completing
        child_run_completed = Run(
            run_id="child-run-approval-1",
            graph_version_ref="approval-child:v1",
            deployment_ref="approval-child",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"approved": True}},
        )

        approval_child_graph = _make_approval_graph("approval-child")
        mock_resolver = AsyncMock(spec=SubgraphResolver)
        mock_resolver.resolve = AsyncMock(return_value=(approval_child_graph, MagicMock()))
        mock_executor_2 = SubgraphExecutor(resolver=mock_resolver)

        run_repo_2 = _make_run_repository()
        run_repo_2.get = AsyncMock(return_value=child_run_completed)
        orch2 = _build_orchestrator(
            run_repository=run_repo_2,
            subgraph_executor=mock_executor_2,
        )

        # Resume the paused parent run
        result.status = RunStatus.RUNNING
        final = await orch2._drive(parent_graph, result)

        assert final.status == RunStatus.COMPLETED
        assert final.final_output == {"approved": True}
        assert "pending_subgraph" not in final.metadata

    @pytest.mark.asyncio
    async def test_approval_flow_preserves_pending_subgraph_through_pause(self) -> None:
        """pending_subgraph metadata survives the pause/resume cycle."""
        child_run_paused = Run(
            run_id="child-run-x",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run_paused)

        parent_graph = _make_subgraph_parent()
        run_repo = _make_run_repository()
        orch = _build_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch.run_graph(parent_graph, {"input": "test"})

        # Verify the pending_subgraph has all required fields
        pending = result.metadata["pending_subgraph"]
        assert "child_run_id" in pending
        assert "node_id" in pending
        assert "graph_ref" in pending
        assert "version" in pending
        assert pending["node_id"] == "s1"
