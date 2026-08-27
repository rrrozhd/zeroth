"""Structured-token fan-out concurrency through real child-run dispatch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel, ConfigDict

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    EntrypointNode,
    ExecutionSettings,
    Graph,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.parallel.models import ParallelConfig
from zeroth.runtime.runs import Run, RunStatus
from zeroth.runtime.subgraphs import SubgraphExecutor
from zeroth.runtime.subgraphs.resolver import SubgraphResolver
from zeroth.service.deployments.models import Deployment


class _SlowPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: int


class _DeploymentLookup:
    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment

    async def get(self, deployment_ref, version=None, **_scope):
        if deployment_ref != self.deployment.deployment_ref:
            return None
        if version is not None and version != self.deployment.graph_version:
            return None
        return self.deployment


class _IntervalSubgraphExecutor(SubgraphExecutor):
    """Real executor with an observable interval around each child dispatch."""

    def __init__(self, resolver: SubgraphResolver, *, delay_seconds: float = 0.025) -> None:
        super().__init__(resolver=resolver)
        self.delay_seconds = delay_seconds
        self.live = 0
        self.peak = 0
        self.child_run_ids: list[str] = []
        self.child_thread_ids: list[str] = []

    async def execute(self, *args, **kwargs):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            # Keep the real invocation interval open long enough for siblings
            # to overlap. ``super().execute`` still resolves, namespaces,
            # creates, checkpoints, and recursively drives a genuine child Run.
            await asyncio.sleep(self.delay_seconds)
            child = await super().execute(*args, **kwargs)
            self.child_run_ids.append(child.run_id)
            self.child_thread_ids.append(child.thread_id)
            return child
        finally:
            self.live -= 1


def _child_deployment() -> Deployment:
    graph = Graph(
        graph_id="token-batch-child-graph",
        name="token-batch-child-graph",
        version=1,
        entry_step="child-entry",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[
            EntrypointNode(
                node_id="child-entry",
                graph_version_ref="token-batch-child-graph@1",
            )
        ],
        edges=[],
    )
    return Deployment(
        deployment_id="token-batch-child-deployment",
        deployment_ref="token-batch-child",
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        serialized_graph=serialize_graph(graph),
    )


def _parent_graph() -> Graph:
    source = EntrypointNode(
        node_id="batch-input",
        graph_version_ref="token-batch-parent@1",
        parallel_config=ParallelConfig(
            split_path="items",
            max_branches=8,
            max_concurrency=4,
            batch_size=8,
            merge_strategy="collect",
            fail_mode="fail_fast",
        ),
    )
    child = SubgraphNode(
        node_id="deterministic-child",
        graph_version_ref="token-batch-parent@1",
        subgraph=SubgraphNodeData(
            graph_ref="token-batch-child",
            version=1,
            thread_participation="isolated",
            max_depth=1,
        ),
    )
    return Graph(
        graph_id="token-batch-parent",
        name="token-batch-parent",
        version=1,
        entry_step=source.node_id,
        execution_settings=ExecutionSettings(
            sequential_join_enabled=True,
            max_total_steps=32,
        ),
        nodes=[source, child],
        edges=[
            Edge(
                edge_id="batch-to-child",
                source_node_id=source.node_id,
                target_node_id=child.node_id,
            )
        ],
    )


def _slow_agent_deployment() -> Deployment:
    graph = Graph(
        graph_id="token-slow-child-graph",
        name="token-slow-child-graph",
        version=1,
        entry_step="slow-agent",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[
            AgentNode(
                node_id="slow-agent",
                graph_version_ref="token-slow-child-graph@1",
                agent=AgentNodeData(instruction="slow", model_provider="test/slow"),
            )
        ],
        edges=[],
    )
    return Deployment(
        deployment_id="token-slow-child-deployment",
        deployment_ref="token-slow-child",
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        serialized_graph=serialize_graph(graph),
    )


def _slow_parent_graph(*, branch_timeout_seconds: float) -> Graph:
    graph = _parent_graph()
    source = graph.nodes[0]
    source.parallel_config = source.parallel_config.model_copy(
        update={"branch_timeout_seconds": branch_timeout_seconds}
    )
    child = graph.nodes[1]
    child.subgraph = child.subgraph.model_copy(
        update={"graph_ref": "token-slow-child", "version": 1}
    )
    return graph


async def test_structured_batch_overlaps_four_real_subgraph_dispatches(sqlite_db) -> None:
    """Eight real child Runs overlap at exactly the authored concurrency cap."""
    repository = RunRepository.for_default_compatibility(sqlite_db)
    executor = _IntervalSubgraphExecutor(
        resolver=SubgraphResolver(_DeploymentLookup(_child_deployment()))
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    )

    run = await orchestrator.run_graph(
        _parent_graph(),
        {"items": [{"value": index} for index in range(8)]},
    )

    assert run.status is RunStatus.COMPLETED, run.error
    assert executor.peak == 4
    assert len(executor.child_run_ids) == 8
    assert len(set(executor.child_run_ids)) == 8
    assert len(set(executor.child_thread_ids)) == 8
    assert [entry.node_id for entry in run.execution_history] == [
        "batch-input",
        *("deterministic-child" for _ in range(8)),
    ]
    assert [
        entry.input_snapshot
        for entry in run.execution_history
        if entry.node_id == "deterministic-child"
    ] == [{"value": index} for index in range(8)]
    assert run.final_output == {"items": [{"value": index} for index in range(8)]}


async def test_repeated_structured_batches_do_not_reuse_child_guard_state(sqlite_db) -> None:
    """Identical no-thread submissions create fresh parent and child isolation."""
    repository = RunRepository.for_default_compatibility(sqlite_db)
    executor = _IntervalSubgraphExecutor(
        resolver=SubgraphResolver(_DeploymentLookup(_child_deployment()))
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    )
    parent_ids: list[str] = []
    parent_threads: list[str] = []

    for _ in range(3):
        run = await orchestrator.run_graph(
            _parent_graph(),
            {"items": [{"value": index} for index in range(8)]},
        )
        assert run.status is RunStatus.COMPLETED, run.error
        assert run.failure_state is None
        parent_ids.append(run.run_id)
        parent_threads.append(run.thread_id)

    assert len(set(parent_ids)) == 3
    assert len(set(parent_threads)) == 3
    assert len(executor.child_run_ids) == 24
    assert len(set(executor.child_run_ids)) == 24
    assert len(set(executor.child_thread_ids)) == 24


async def test_structured_batch_advances_once_from_ordered_fan_in(sqlite_db) -> None:
    """A post-batch successor consumes one branch-index-ordered aggregate."""
    base = _parent_graph()
    collector = EntrypointNode(
        node_id="collector",
        graph_version_ref="token-batch-parent@1",
    )
    graph = base.model_copy(
        update={
            "nodes": [*base.nodes, collector],
            "edges": [
                *base.edges,
                Edge(
                    edge_id="child-to-collector",
                    source_node_id="deterministic-child",
                    target_node_id=collector.node_id,
                ),
            ],
        }
    )
    repository = RunRepository.for_default_compatibility(sqlite_db)
    executor = _IntervalSubgraphExecutor(
        resolver=SubgraphResolver(_DeploymentLookup(_child_deployment()))
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    )

    run = await orchestrator.run_graph(
        graph,
        {"items": [{"value": index} for index in range(8)]},
    )

    assert run.status is RunStatus.COMPLETED, run.error
    assert executor.peak == 4
    assert [entry.node_id for entry in run.execution_history] == [
        "batch-input",
        *("deterministic-child" for _ in range(8)),
        "collector",
    ]
    assert run.execution_history[-1].input_snapshot == {
        "items": [{"value": index} for index in range(8)]
    }
    assert run.final_output == {"items": [{"value": index} for index in range(8)]}


async def test_branch_timeout_starts_after_concurrency_slot_acquisition(sqlite_db) -> None:
    """Queued siblings do not spend their execution timeout waiting for a slot."""
    graph = _parent_graph()
    source = graph.nodes[0]
    source.parallel_config = source.parallel_config.model_copy(
        update={"branch_timeout_seconds": 6.0}
    )
    repository = RunRepository.for_default_compatibility(sqlite_db)
    executor = _IntervalSubgraphExecutor(
        resolver=SubgraphResolver(_DeploymentLookup(_child_deployment())),
        delay_seconds=3.5,
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    )

    run = await orchestrator.run_graph(
        graph,
        {"items": [{"value": index} for index in range(8)]},
    )

    assert run.status is RunStatus.COMPLETED, run.error
    assert executor.peak == 4
    assert len(executor.child_run_ids) == 8


async def test_timeout_fail_fast_terminalizes_started_child_runs(sqlite_db) -> None:
    """Timed-out and sibling-cancelled child Runs never remain RUNNING."""
    repository = RunRepository.for_default_compatibility(sqlite_db)

    async def slow_provider(request):
        await asyncio.sleep(10)
        return ProviderResponse(content=request.metadata["input_payload"])

    runner = AgentRunner(
        AgentConfig(
            name="slow-child",
            instruction="slow",
            model_name="test/slow",
            input_model=_SlowPayload,
            output_model=_SlowPayload,
        ),
        CallableProviderAdapter(slow_provider),
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={"subgraph:token-slow-child:1:slow-agent": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=SubgraphExecutor(
            resolver=SubgraphResolver(_DeploymentLookup(_slow_agent_deployment()))
        ),
    )

    parent = await orchestrator.run_graph(
        _slow_parent_graph(branch_timeout_seconds=0.5),
        {"items": [{"value": index} for index in range(8)]},
    )

    assert parent.status is RunStatus.FAILED
    assert parent.failure_state is not None
    assert "TimeoutError" in (parent.failure_state.message or "")
    children = await repository.list_child_runs(parent.run_id)
    assert len(children) == 4
    assert all(child.status is RunStatus.FAILED for child in children), [
        (child.run_id, child.status, child.failure_state) for child in children
    ]
    assert {child.failure_state.reason for child in children if child.failure_state} == {
        "parallel_branch_cancelled"
    }


async def test_paused_parallel_child_resumes_without_reexecuting_completed_sibling(
    sqlite_db,
) -> None:
    """The source claim survives approval and resumes only its paused child."""
    graph = _parent_graph()
    source = graph.nodes[0]
    source.parallel_config = source.parallel_config.model_copy(update={"fail_mode": "best_effort"})

    async def execute_child(**kwargs):
        index = kwargs["branch_context"].branch_index
        return Run(
            run_id=f"approval-child-{index}",
            graph_version_ref="token-batch-child-graph:v1",
            deployment_ref="token-batch-child",
            status=(RunStatus.COMPLETED if index == 0 else RunStatus.WAITING_APPROVAL),
            final_output={"value": index} if index == 0 else None,
        )

    executor = MagicMock(spec=SubgraphExecutor)
    executor.execute = AsyncMock(side_effect=execute_child)
    executor.resume = AsyncMock(
        return_value=Run(
            run_id="approval-child-1",
            graph_version_ref="token-batch-child-graph:v1",
            deployment_ref="token-batch-child",
            status=RunStatus.COMPLETED,
            final_output={"value": 1},
        )
    )
    repository = RunRepository.for_default_compatibility(sqlite_db)
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    )

    waiting = await orchestrator.run_graph(
        graph,
        {"items": [{"value": 0}, {"value": 1}]},
    )

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert (
        waiting.metadata["pending_parallel_subgraph"]["paused_branch"]["child_run_id"]
        == "approval-child-1"
    )
    snapshot = await repository.get_token_snapshot(waiting.run_id)
    assert snapshot is not None
    assert len(snapshot.in_flight_dispatches) == 1

    waiting.status = RunStatus.RUNNING
    resumed = await orchestrator._drive(graph, waiting)

    assert resumed.status is RunStatus.COMPLETED, resumed.error
    assert [entry.node_id for entry in resumed.execution_history] == [
        "batch-input",
        "deterministic-child",
        "deterministic-child",
    ]
    assert executor.execute.await_count == 2
    executor.resume.assert_awaited_once()
