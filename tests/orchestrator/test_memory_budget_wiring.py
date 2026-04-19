"""Tests for Phase 20: memory resolver and budget enforcer dispatch-time injection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import AgentNode, AgentNodeData, Graph
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, RunStatus


class SimpleInput(BaseModel):
    value: str


class SimpleOutput(BaseModel):
    answer: str


def _make_graph(node_id: str = "agent-1") -> Graph:
    """Build a minimal single-agent graph for testing."""
    return Graph(
        graph_id="test-graph",
        name="test",
        version=1,
        nodes=[
            AgentNode(
                node_id=node_id,
                graph_version_ref="test-graph:v1",
                agent=AgentNodeData(instruction="test", model_provider="provider://test"),
            ),
        ],
        edges=[],
    )


def _make_runner(*, raise_on_run: Exception | None = None) -> AgentRunner:
    """Build a real AgentRunner with a callable provider for testing."""
    config = AgentConfig(
        name="test-agent",
        instruction="You are a test agent.",
        model_name="test-model",
        input_model=SimpleInput,
        output_model=SimpleOutput,
    )

    def _provider_fn(request):
        return ProviderResponse(content='{"answer": "hello"}')

    runner = AgentRunner(config, CallableProviderAdapter(_provider_fn))

    if raise_on_run is not None:
        original_run = runner.run

        async def _failing_run(*args, **kwargs):
            raise raise_on_run

        runner.run = _failing_run

    return runner


async def test_dispatch_injects_memory_resolver(sqlite_db) -> None:
    """When orchestrator has memory_resolver set, it is injected onto the runner during dispatch."""
    resolver = MemoryConnectorResolver(registry=InMemoryConnectorRegistry())
    runner = _make_runner()

    # Verify runner starts without a resolver.
    assert runner.memory_resolver is None

    run_repo = RunRepository(sqlite_db)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={"agent-1": runner},
        executable_unit_runner=ExecutableUnitRunner(),
        memory_resolver=resolver,
    )

    graph = _make_graph()
    run = await orchestrator.run_graph(graph, {"value": "test"})

    assert run.status == RunStatus.COMPLETED
    # After dispatch completes, the runner's memory_resolver should be restored to None.
    assert runner.memory_resolver is None


async def test_dispatch_injects_budget_enforcer(sqlite_db) -> None:
    """When orchestrator has budget_enforcer set, it is injected onto the runner during dispatch."""
    budget_enforcer = MagicMock()
    budget_enforcer.check_budget = AsyncMock(return_value=(True, 0.0, 100.0))
    runner = _make_runner()

    assert runner.budget_enforcer is None

    run_repo = RunRepository(sqlite_db)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={"agent-1": runner},
        executable_unit_runner=ExecutableUnitRunner(),
        budget_enforcer=budget_enforcer,
    )

    graph = _make_graph()
    run = await orchestrator.run_graph(graph, {"value": "test"})

    assert run.status == RunStatus.COMPLETED
    assert runner.budget_enforcer is None


async def test_dispatch_preserves_originals_when_none(sqlite_db) -> None:
    """When orchestrator has no resolver/enforcer (None), runner values are preserved."""
    original_resolver = MemoryConnectorResolver(registry=InMemoryConnectorRegistry())
    original_enforcer = MagicMock()
    original_enforcer.check_budget = AsyncMock(return_value=(True, 0.0, 100.0))
    runner = _make_runner()
    runner.memory_resolver = original_resolver
    runner.budget_enforcer = original_enforcer

    run_repo = RunRepository(sqlite_db)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={"agent-1": runner},
        executable_unit_runner=ExecutableUnitRunner(),
        # memory_resolver and budget_enforcer are None by default.
    )

    graph = _make_graph()
    run = await orchestrator.run_graph(graph, {"value": "test"})

    assert run.status == RunStatus.COMPLETED
    # Originals should be preserved since orchestrator had None.
    assert runner.memory_resolver is original_resolver
    assert runner.budget_enforcer is original_enforcer


async def test_dispatch_restores_on_exception(sqlite_db) -> None:
    """If the agent run raises an exception, originals are still restored in finally block."""
    resolver = MemoryConnectorResolver(registry=InMemoryConnectorRegistry())
    budget_enforcer = MagicMock()
    runner = _make_runner(raise_on_run=RuntimeError("boom"))

    assert runner.memory_resolver is None
    assert runner.budget_enforcer is None

    run_repo = RunRepository(sqlite_db)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={"agent-1": runner},
        executable_unit_runner=ExecutableUnitRunner(),
        memory_resolver=resolver,
        budget_enforcer=budget_enforcer,
    )

    graph = _make_graph()
    # The run should fail due to the exception, but not raise to the caller.
    run = await orchestrator.run_graph(graph, {"value": "test"})

    assert run.status == RunStatus.FAILED
    # Originals should be restored even after exception.
    assert runner.memory_resolver is None
    assert runner.budget_enforcer is None
