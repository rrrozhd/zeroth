"""Integration tests for SubgraphNode detection in _drive() and bootstrap wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.platform.measurement import MeasurementState
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
    SubgraphNode,
)
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import Run, RunStatus
from zeroth.runtime.subgraphs.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.runtime.subgraphs.executor import SubgraphExecutor
from zeroth.runtime.subgraphs.models import SubgraphNodeData
from zeroth.runtime.subgraphs.resolver import SubgraphResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_child_graph(
    graph_id: str = "child-g",
    entry_step: str = "c1",
) -> Graph:
    """Create a simple child graph for testing."""
    node = AgentNode(
        node_id="c1",
        graph_version_ref=f"{graph_id}@1",
        agent=AgentNodeData(
            instruction="child task",
            model_provider="openai/gpt-4",
        ),
    )
    return Graph(
        graph_id=graph_id,
        name="child-workflow",
        version=1,
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=[node],
        edges=[],
        entry_step=entry_step,
    )


def _make_parent_graph_with_subgraph(
    subgraph_ref: str = "child-g",
    entry_step: str = "s1",
) -> Graph:
    """Create a parent graph containing a SubgraphNode."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref="parent-g@1",
        subgraph=SubgraphNodeData(graph_ref=subgraph_ref),
    )
    return Graph(
        graph_id="parent-g",
        name="parent-workflow",
        version=1,
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=[subgraph_node],
        edges=[],
        entry_step=entry_step,
    )


def _make_parent_graph_with_subgraph_and_successor(
    subgraph_ref: str = "child-g",
) -> Graph:
    """Parent graph: SubgraphNode -> AgentNode (to test output propagation)."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref="parent-g@1",
        subgraph=SubgraphNodeData(graph_ref=subgraph_ref),
    )
    agent_node = AgentNode(
        node_id="a1",
        graph_version_ref="parent-g@1",
        agent=AgentNodeData(
            instruction="use subgraph output",
            model_provider="openai/gpt-4",
        ),
    )
    edge = Edge(
        edge_id="e1",
        source_node_id="s1",
        target_node_id="a1",
    )
    return Graph(
        graph_id="parent-g",
        name="parent-workflow",
        version=1,
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=[subgraph_node, agent_node],
        edges=[edge],
        entry_step="s1",
    )


def _make_run(
    graph: Graph,
    pending_node_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> Run:
    """Create a Run for the given graph."""
    entry = graph.entry_step or graph.nodes[0].node_id
    default_metadata = {
        "graph_id": graph.graph_id,
        "graph_name": graph.name,
        "node_payloads": {entry: {"input": "test"}},
        "edge_visit_counts": {},
        "path": [],
        "audits": {},
    }
    if metadata:
        default_metadata.update(metadata)
    return Run(
        run_id="run-parent-1",
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        deployment_ref=graph.graph_id,
        thread_id="thread-1",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        status=RunStatus.RUNNING,
        pending_node_ids=pending_node_ids or [entry],
        metadata=default_metadata,
    )


def _make_run_repository() -> AsyncMock:
    """Create a mock RunRepository."""
    repo = AsyncMock()
    repo.create = AsyncMock(side_effect=lambda r: r)
    repo.put = AsyncMock(side_effect=lambda r: r)
    repo.get = AsyncMock(return_value=None)
    repo.write_checkpoint = AsyncMock()
    return repo


def _make_orchestrator(
    run_repository: AsyncMock | None = None,
    subgraph_executor: SubgraphExecutor | None = None,
) -> RuntimeOrchestrator:
    """Create a RuntimeOrchestrator with mocked dependencies."""
    from zeroth.integrations.execution import ExecutableUnitRunner

    return RuntimeOrchestrator(
        run_repository=run_repository or _make_run_repository(),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(),
        subgraph_executor=subgraph_executor,
    )


# ---------------------------------------------------------------------------
# Tests: RuntimeOrchestrator field
# ---------------------------------------------------------------------------


class TestOrchestratorSubgraphField:
    """RuntimeOrchestrator has subgraph_executor field (optional, None default)."""

    def test_subgraph_executor_defaults_to_none(self) -> None:
        from zeroth.integrations.execution import ExecutableUnitRunner

        orch = RuntimeOrchestrator(
            run_repository=AsyncMock(),
            agent_runners={},
            executable_unit_runner=ExecutableUnitRunner(),
        )
        assert orch.subgraph_executor is None

    def test_subgraph_executor_can_be_set(self) -> None:
        resolver = MagicMock(spec=SubgraphResolver)
        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator(subgraph_executor=executor)
        assert orch.subgraph_executor is executor


# ---------------------------------------------------------------------------
# Tests: _drive() SubgraphNode detection
# ---------------------------------------------------------------------------


class TestDriveSubgraphNode:
    """_drive() encountering SubgraphNode calls subgraph_executor.execute()."""

    pytestmark = pytest.mark.legacy_engine

    @pytest.mark.asyncio
    async def test_drive_calls_subgraph_executor_execute(self) -> None:
        """SubgraphNode triggers subgraph_executor.execute() with correct args."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"result": "child-done"},
            metadata={},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)  # noqa: F841

        mock_executor.execute.assert_called_once()
        call_kwargs = mock_executor.execute.call_args[1]
        assert call_kwargs["orchestrator"] is orch
        assert call_kwargs["parent_graph"] is parent_graph
        assert call_kwargs["parent_run"] is parent_run
        assert call_kwargs["node_id"] == "s1"

    @pytest.mark.asyncio
    async def test_drive_subgraph_no_executor_fails_run(self) -> None:
        """SubgraphNode with subgraph_executor=None fails the run."""
        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=None)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED
        assert "SubgraphExecutor not configured" in (result.failure_state.message or "")

    @pytest.mark.asyncio
    async def test_drive_subgraph_uses_child_final_output(self) -> None:
        """After SubgraphNode, child_run.final_output becomes node output."""
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
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        # Parent run should complete with child's output
        assert result.status == RunStatus.COMPLETED
        assert result.final_output == {"answer": 42}

    @pytest.mark.asyncio
    async def test_drive_subgraph_records_history(self) -> None:
        """SubgraphNode execution records audit history with child run_id."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"result": "ok"},
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        # Should have at least one history entry for the subgraph node
        assert len(result.execution_history) >= 1
        history = result.execution_history[0]
        assert history.node_id == "s1"

    @pytest.mark.asyncio
    async def test_drive_subgraph_promotes_separate_child_cost_provenance(self) -> None:
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.COMPLETED,
            final_output={"result": "ok"},
            metadata={
                "subgraph_depth": 1,
                "total_cost_usd": 0.2,
                "total_estimated_cost_usd": 0.3,
                "cost_measurement": MeasurementState.ESTIMATED,
            },
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)
        parent_graph = _make_parent_graph_with_subgraph()

        result = await _make_orchestrator(
            run_repository=_make_run_repository(),
            subgraph_executor=mock_executor,
        )._drive(parent_graph, _make_run(parent_graph))

        (history,) = result.execution_history
        assert history.cost_usd == 0.2
        assert history.estimated_cost_usd == 0.3
        assert history.cost_measurement is MeasurementState.ESTIMATED


# ---------------------------------------------------------------------------
# Tests: _drive() error handling
# ---------------------------------------------------------------------------


class TestDriveSubgraphErrors:
    """_drive() handles SubgraphDepthLimitError, SubgraphResolutionError by failing run."""

    pytestmark = pytest.mark.legacy_engine

    @pytest.mark.asyncio
    async def test_depth_limit_error_fails_run(self) -> None:
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(
            side_effect=SubgraphDepthLimitError("depth 4 exceeds max_depth 3")
        )

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED
        assert "depth 4 exceeds max_depth 3" in (result.failure_state.message or "")

    @pytest.mark.asyncio
    async def test_resolution_error_fails_run(self) -> None:
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=SubgraphResolutionError("not found"))

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED
        assert "not found" in (result.failure_state.message or "")

    @pytest.mark.asyncio
    async def test_cycle_error_fails_run(self) -> None:
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=SubgraphCycleError("circular reference"))

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED

    @pytest.mark.asyncio
    async def test_execution_error_fails_run(self) -> None:
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=SubgraphExecutionError("boom"))

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED


# ---------------------------------------------------------------------------
# Tests: Bootstrap wiring
# ---------------------------------------------------------------------------


class TestBootstrapSubgraphWiring:
    """ServiceBootstrap has subgraph_executor field and bootstrap_service wires it."""

    def test_service_bootstrap_has_subgraph_executor_field(self) -> None:
        # Check the dataclass field exists
        import dataclasses

        from zeroth.service.bootstrap import ServiceBootstrap

        field_names = [f.name for f in dataclasses.fields(ServiceBootstrap)]
        assert "subgraph_executor" in field_names

    def test_service_bootstrap_subgraph_executor_default_none(self) -> None:
        """ServiceBootstrap.subgraph_executor defaults to None."""
        import dataclasses

        from zeroth.service.bootstrap import ServiceBootstrap

        field = next(
            f for f in dataclasses.fields(ServiceBootstrap) if f.name == "subgraph_executor"
        )
        assert field.default is None
