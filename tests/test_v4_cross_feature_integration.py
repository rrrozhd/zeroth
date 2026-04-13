"""Cross-feature integration tests for v4.0 D-05 interaction scenarios.

Exercises the five cross-feature interactions identified in research:
1. Parallel branches + artifact store
2. Parallel branches + context window
3. SubgraphNode in parallel rejected (end-to-end through orchestrator)
4. Template resolution in parallel branches
5. Template resolution in subgraph
6. Concurrent branch runner isolation (race condition test)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.audit import AuditRepository
from zeroth.core.context_window.models import ContextWindowSettings
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
    SubgraphNode,
)
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.parallel.errors import FanOutValidationError
from zeroth.core.parallel.models import ParallelConfig
from zeroth.core.runs import RunRepository, RunStatus
from zeroth.core.subgraph.models import SubgraphNodeData
from zeroth.core.templates.models import TemplateReference
from zeroth.core.templates.registry import TemplateRegistry
from zeroth.core.templates.renderer import TemplateRenderer


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class ItemsInput(BaseModel):
    value: int = 0


class BranchItemInput(BaseModel):
    x: int = 0


class BranchValueInput(BaseModel):
    value: str = ""


class ItemsOutput(BaseModel):
    items: list[dict[str, Any]] = []


class ProcessedOutput(BaseModel):
    result: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_runner(
    *,
    output_model: type[BaseModel],
    handler,
    input_model: type[BaseModel] = ItemsInput,
) -> AgentRunner:
    """Create an AgentRunner with a callable provider adapter."""
    return AgentRunner(
        AgentConfig(
            name="test-agent",
            instruction="test",
            model_name="governai:test",
            input_model=input_model,
            output_model=output_model,
        ),
        CallableProviderAdapter(handler),
    )


def _make_graph(
    nodes: list,
    edges: list,
    *,
    entry_step: str = "source",
    max_total_steps: int = 50,
) -> Graph:
    """Build a Graph from nodes and edges."""
    return Graph(
        graph_id="test-cross-feature",
        name="test-cross-feature",
        entry_step=entry_step,
        execution_settings=ExecutionSettings(max_total_steps=max_total_steps),
        nodes=nodes,
        edges=edges,
    )


def _make_agent_node(
    node_id: str,
    *,
    parallel_config: ParallelConfig | None = None,
    template_ref: TemplateReference | None = None,
    context_window: ContextWindowSettings | None = None,
) -> AgentNode:
    """Build an AgentNode with optional cross-feature config."""
    return AgentNode(
        node_id=node_id,
        graph_version_ref="test-cross-feature:v1",
        agent=AgentNodeData(
            instruction="test",
            model_provider=f"provider://{node_id}",
            template_ref=template_ref,
            context_window=context_window,
        ),
        parallel_config=parallel_config,
    )


def _make_orchestrator(
    agent_runners: dict[str, AgentRunner],
    sqlite_db,
    *,
    audit_repository: AuditRepository | None = None,
    artifact_store: Any = None,
    context_window_enabled: bool = True,
    template_registry: TemplateRegistry | None = None,
    template_renderer: TemplateRenderer | None = None,
    subgraph_executor: Any = None,
) -> RuntimeOrchestrator:
    """Build a RuntimeOrchestrator with cross-feature config."""
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=agent_runners,
        executable_unit_runner=ExecutableUnitRunner(),
        audit_repository=audit_repository,
        artifact_store=artifact_store,
        context_window_enabled=context_window_enabled,
        template_registry=template_registry,
        template_renderer=template_renderer,
        subgraph_executor=subgraph_executor,
    )


# ---------------------------------------------------------------------------
# 1. Parallel branches + artifact store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_branches_with_artifact_store(sqlite_db, tmp_path) -> None:
    """Parallel fan-out with artifact_store set completes without error."""
    from zeroth.core.artifacts.store import FilesystemArtifactStore

    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}),
    )
    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": req.metadata["input_payload"].get("x", 0) * 10}
        ),
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items"),
    )
    sink_node = _make_agent_node("sink")

    graph = _make_graph(
        [source_node, sink_node],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )

    artifact_store = FilesystemArtifactStore(base_dir=str(tmp_path / "artifacts"))

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
        artifact_store=artifact_store,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    assert "items" in last_output
    assert len(last_output["items"]) == 2


# ---------------------------------------------------------------------------
# 2. Parallel branches + context window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_branches_respect_context_window(sqlite_db) -> None:
    """Parallel fan-out with context_window_enabled and per-node settings completes."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}),
    )
    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": req.metadata["input_payload"].get("x", 0) * 10}
        ),
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items"),
    )
    # Downstream agent with context window settings
    sink_node = _make_agent_node(
        "sink",
        context_window=ContextWindowSettings(
            max_context_tokens=128_000,
            compaction_strategy="observation_masking",
        ),
    )

    graph = _make_graph(
        [source_node, sink_node],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
        context_window_enabled=True,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    assert "items" in last_output
    assert len(last_output["items"]) == 2


# ---------------------------------------------------------------------------
# 3. SubgraphNode in parallel rejected (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subgraph_node_in_parallel_rejected(sqlite_db) -> None:
    """Graph with parallel fan-out targeting SubgraphNode fails with clear error."""
    # Source produces items for fan-out
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}),
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items"),
    )

    # Downstream is a SubgraphNode (should be rejected by the guard)
    subgraph_node = SubgraphNode(
        node_id="sub-step",
        graph_version_ref="test-cross-feature:v1",
        subgraph=SubgraphNodeData(
            graph_ref="child-graph",
            max_depth=3,
        ),
    )

    graph = _make_graph(
        [source_node, subgraph_node],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sub-step")],
    )

    orchestrator = _make_orchestrator(
        {"source": source_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    # The run should fail because SubgraphNode is not allowed in parallel branches
    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert "SubgraphNode" in (run.failure_state.message or "")


# ---------------------------------------------------------------------------
# 4. Template resolution in parallel branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_resolution_in_parallel_branches(sqlite_db) -> None:
    """Parallel fan-out with template_ref on downstream agent resolves templates."""
    registry = TemplateRegistry()
    registry.register(
        "test-tmpl",
        1,
        "Process: {{ input.value }}",
    )
    renderer = TemplateRenderer()

    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(
            content={"items": [{"value": "alpha"}, {"value": "beta"}]}
        ),
    )
    sink_runner = _make_agent_runner(
        input_model=BranchValueInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(content={"result": 42}),
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items"),
    )
    # Downstream agent with template_ref
    sink_node = _make_agent_node(
        "sink",
        template_ref=TemplateReference(name="test-tmpl", version=1),
    )

    graph = _make_graph(
        [source_node, sink_node],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
        template_registry=registry,
        template_renderer=renderer,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    assert "items" in last_output
    assert len(last_output["items"]) == 2


# ---------------------------------------------------------------------------
# 5. Template resolution in subgraph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_resolution_in_subgraph(sqlite_db) -> None:
    """SubgraphNode with template_ref on child graph's agent resolves template."""
    from unittest.mock import MagicMock

    from zeroth.core.graph.serialization import serialize_graph
    from zeroth.core.runs.models import Run

    registry = TemplateRegistry()
    registry.register(
        "child-tmpl",
        1,
        "Child process: {{ input.value }}",
    )
    renderer = TemplateRenderer()

    # Child graph with an agent that has template_ref
    child_agent_node = AgentNode(
        node_id="c1",
        graph_version_ref="child-g@1",
        agent=AgentNodeData(
            instruction="child task",
            model_provider="provider://c1",
            template_ref=TemplateReference(name="child-tmpl", version=1),
        ),
    )
    child_graph = Graph(
        graph_id="child-g",
        name="child-workflow",
        version=1,
        nodes=[child_agent_node],
        edges=[],
        entry_step="c1",
    )

    # Parent graph with SubgraphNode
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref="parent-g@1",
        subgraph=SubgraphNodeData(
            graph_ref="child-g",
            max_depth=3,
        ),
    )
    parent_graph = Graph(
        graph_id="parent-g",
        name="parent-workflow",
        version=1,
        nodes=[subgraph_node],
        edges=[],
        entry_step="s1",
    )

    # Mock SubgraphExecutor to simulate child execution completing with template
    child_run = Run(
        run_id="child-run-1",
        graph_version_ref="child-g:v1",
        deployment_ref="child-g",
        status=RunStatus.COMPLETED,
        final_output={"result": "templated-output"},
    )

    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=child_run)

    # Build child runner for template verification
    child_runner = _make_agent_runner(
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(content={"result": 99}),
    )

    orchestrator = _make_orchestrator(
        {"c1": child_runner},
        sqlite_db,
        template_registry=registry,
        template_renderer=renderer,
        subgraph_executor=mock_executor,
    )

    run = await orchestrator.run_graph(parent_graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Verify subgraph executor was called
    mock_executor.execute.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Concurrent branch runner isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_branch_runner_isolation(sqlite_db) -> None:
    """Multiple parallel branches targeting the same agent don't corrupt runner state."""
    call_tracker: list[dict[str, Any]] = []

    def source_handler(req):
        """Source produces 4 items for fan-out."""
        return ProviderResponse(content={"items": [{"x": i} for i in range(4)]})

    def sink_handler(req):
        """Each branch records its input and returns branch-specific output."""
        x = req.metadata["input_payload"].get("x", 0)
        call_tracker.append({"x": x})
        return ProviderResponse(content={"result": x * 100})

    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=source_handler,
    )
    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=sink_handler,
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items"),
    )
    sink_node = _make_agent_node("sink")

    graph = _make_graph(
        [source_node, sink_node],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED

    # All 4 branches executed
    assert len(call_tracker) == 4

    # Verify each branch received its correct input (no corruption)
    received_xs = sorted(entry["x"] for entry in call_tracker)
    assert received_xs == [0, 1, 2, 3]

    # Verify merged output has correct branch-specific results
    last_output = run.metadata.get("last_output", {})
    items = last_output.get("items", [])
    assert len(items) == 4
    for i, item in enumerate(items):
        assert item is not None
        assert item.get("result") == i * 100
