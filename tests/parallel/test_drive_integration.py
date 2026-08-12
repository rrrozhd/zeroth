"""Integration tests for _drive() loop with parallel fan-out.

Tests that the RuntimeOrchestrator correctly detects parallel_config on nodes,
delegates to ParallelExecutor for fan-out/fan-in, and merges branch state back
into the parent Run. Also verifies that sequential execution is completely
unchanged when parallel_config is None.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.execution import (
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.orchestration.parallel_executor import sum_run_cost
from zeroth.runtime.parallel.models import ParallelConfig
from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.runs import Run, RunFailureState, RunStatus

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class ItemsInput(BaseModel):
    value: int = 0


class BranchItemInput(BaseModel):
    x: int = 0


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
        graph_id="test-parallel",
        name="test-parallel",
        entry_step=entry_step,
        execution_settings=ExecutionSettings(max_total_steps=max_total_steps),
        nodes=nodes,
        edges=edges,
    )


def _make_agent_node(
    node_id: str,
    *,
    parallel_config: ParallelConfig | None = None,
) -> AgentNode:
    """Build an AgentNode with optional parallel_config."""
    return AgentNode(
        node_id=node_id,
        graph_version_ref="test-parallel:v1",
        agent=AgentNodeData(instruction="test", model_provider=f"provider://{node_id}"),
        parallel_config=parallel_config,
    )


def _make_orchestrator(
    agent_runners: dict[str, AgentRunner],
    sqlite_db,
    *,
    audit_repository: AuditRepository | None = None,
) -> RuntimeOrchestrator:
    """Build a RuntimeOrchestrator with minimal config."""
    eu_registry = ExecutableUnitRegistry()
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=agent_runners,
        executable_unit_runner=ExecutableUnitRunner(eu_registry),
        audit_repository=audit_repository,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_unchanged(sqlite_db) -> None:
    """Graph with NO parallel_config runs identically to before (backward compat)."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}]}),
    )
    sink_runner = _make_agent_runner(
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": req.metadata["input_payload"].get("x", 0) + 10}
        ),
    )

    source_node = _make_agent_node("source")
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
    assert len(run.execution_history) == 2
    assert [e.node_id for e in run.execution_history] == ["source", "sink"]


@pytest.mark.asyncio
async def test_fan_out_basic(sqlite_db) -> None:
    """Fan-out node produces items, downstream processes each, results merged."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}),
    )
    # The downstream runner processes each item from the fan-out
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

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # The merged output should contain 2 results at the "items" path
    last_output = run.metadata.get("last_output", {})
    assert "items" in last_output
    assert len(last_output["items"]) == 2


@pytest.mark.asyncio
async def test_fan_out_fan_in_ordering(sqlite_db) -> None:
    """5-item fan-out produces results ordered by branch index."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": i} for i in range(5)]}),
    )
    # Each branch multiplies x by 10 -- ordering should be preserved
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

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    items = last_output.get("items", [])
    assert len(items) == 5
    # Results should be ordered by branch index (0, 1, 2, 3, 4)
    for i, item in enumerate(items):
        assert item is not None
        assert item.get("result") == i * 10


@pytest.mark.asyncio
async def test_fan_out_best_effort(sqlite_db) -> None:
    """3 branches, 1 fails, run still completes in best-effort mode."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": -1}, {"x": 3}]}),
    )

    def sink_handler(req):
        x = req.metadata["input_payload"].get("x", 0)
        if x == -1:
            raise RuntimeError("branch failure")
        return ProviderResponse(content={"result": x * 10})

    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=sink_handler,
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items", fail_mode="best_effort"),
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
    last_output = run.metadata.get("last_output", {})
    items = last_output.get("items", [])
    assert len(items) == 3
    # Failed branch at index 1 should be None
    assert items[0] is not None
    assert items[1] is None
    assert items[2] is not None


@pytest.mark.asyncio
async def test_fan_out_fail_fast(sqlite_db) -> None:
    """3 branches, 1 fails, run fails with ParallelExecutionError in fail-fast mode."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": -1}, {"x": 3}]}),
    )

    def sink_handler(req):
        x = req.metadata["input_payload"].get("x", 0)
        if x == -1:
            raise RuntimeError("branch failure")
        return ProviderResponse(content={"result": x * 10})

    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=sink_handler,
    )

    source_node = _make_agent_node(
        "source",
        parallel_config=ParallelConfig(split_path="items", fail_mode="fail_fast"),
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

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert "parallel_execution_failed" in run.failure_state.reason


@pytest.mark.asyncio
async def test_fan_out_fail_fast_persists_paid_failed_branch_history(sqlite_db) -> None:
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}]}),
    )
    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": "not-an-integer"},
            estimated_cost_usd=0.25,
            cost_measurement=MeasurementState.ESTIMATED,
        ),
    )
    graph = _make_graph(
        [
            _make_agent_node(
                "source",
                parallel_config=ParallelConfig(split_path="items", fail_mode="fail_fast"),
            ),
            _make_agent_node("sink"),
        ],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    graph.execution_settings.sequential_join_enabled = False

    run = await _make_orchestrator(
        {"source": source_runner, "sink": sink_runner}, sqlite_db
    ).run_graph(graph, {"value": 1})

    assert run.status is RunStatus.FAILED
    failed = [entry for entry in run.execution_history if entry.status == "rejected"]
    assert len(failed) == 1, [
        (entry.node_id, entry.status, entry.estimated_cost_usd) for entry in run.execution_history
    ]
    assert failed[0].estimated_cost_usd == 0.25
    assert failed[0].cost_measurement is MeasurementState.ESTIMATED
    assert sum_run_cost(run) == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_parallel_config_none_no_effect(sqlite_db) -> None:
    """Explicit parallel_config=None behaves identically to no parallel_config."""
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}]}),
    )
    sink_runner = _make_agent_runner(
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(content={"result": 42}),
    )

    source_node = _make_agent_node("source", parallel_config=None)
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
    assert len(run.execution_history) == 2
    assert [e.node_id for e in run.execution_history] == ["source", "sink"]


@pytest.mark.asyncio
async def test_fan_out_history_merged(sqlite_db) -> None:
    """Branch execution_history entries appear in parent run after fan-in."""
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

    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Source node history entry + 2 branch history entries = at least 3
    assert len(run.execution_history) >= 3
    # The source node should appear in history
    node_ids = [e.node_id for e in run.execution_history]
    assert "source" in node_ids
    # Branch entries for "sink" should also appear (one per branch)
    sink_entries = [e for e in run.execution_history if e.node_id == "sink"]
    assert len(sink_entries) == 2


@pytest.mark.asyncio
async def test_fan_out_promotes_branch_cost_measurement(sqlite_db) -> None:
    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(
            content={"items": [{"x": 1}, {"x": 2}]},
            cost_usd=0.0,
            cost_measurement=MeasurementState.MEASURED,
        ),
    )
    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": req.metadata["input_payload"].get("x", 0)},
            estimated_cost_usd=0.25,
            cost_measurement=MeasurementState.ESTIMATED,
        ),
    )
    graph = _make_graph(
        [
            _make_agent_node("source", parallel_config=ParallelConfig(split_path="items")),
            _make_agent_node("sink"),
        ],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )

    audit_repo = AuditRepository(sqlite_db)
    run = await _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
        audit_repository=audit_repo,
    ).run_graph(graph, {"value": 1})

    sink_entries = [entry for entry in run.execution_history if entry.node_id == "sink"]
    assert [entry.estimated_cost_usd for entry in sink_entries] == [0.25, 0.25]
    assert all(entry.cost_measurement is MeasurementState.ESTIMATED for entry in sink_entries)
    sink_audits = [
        record for record in await audit_repo.list_by_run(run.run_id) if record.node_id == "sink"
    ]
    assert [record.estimated_cost_usd for record in sink_audits] == [0.25, 0.25]
    assert all(record.cost_measurement is MeasurementState.ESTIMATED for record in sink_audits)
    assert sum_run_cost(run) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_fan_out_audit_refs_merged(sqlite_db) -> None:
    """Branch audit_refs are appended to parent run after fan-in."""
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

    audit_repo = AuditRepository(sqlite_db)
    orchestrator = _make_orchestrator(
        {"source": source_runner, "sink": sink_runner},
        sqlite_db,
        audit_repository=audit_repo,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Source audit + 2 branch audits = at least 3 audit refs
    assert len(run.audit_refs) >= 3


@pytest.mark.asyncio
async def test_operator_cancel_during_fan_out_stops_before_downstream(sqlite_db) -> None:
    """Audit F3 (fan-out guard): an operator cancel that lands while parallel.

    branches are running is observed at the fan-in, so the downstream node after
    the fan-in never dispatches and the run ends FAILED.
    """
    repo = RunRepository(sqlite_db)
    run_id_box: dict[str, str] = {}

    source_runner = _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}),
    )

    async def _cancel_branch(req):
        # Operator cancels the parent run mid-fan-out (as the admin API would).
        await repo.transition(
            run_id_box["id"],
            RunStatus.FAILED,
            failure_state=RunFailureState(reason="operator_cancelled", message="cancelled"),
        )
        return ProviderResponse(content={"result": 1})

    branch_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=_cancel_branch,
    )

    final_calls = {"n": 0}

    def _final_handler(req):
        final_calls["n"] += 1
        return ProviderResponse(content={"result": 0})

    final_runner = _make_agent_runner(output_model=ProcessedOutput, handler=_final_handler)

    source_node = _make_agent_node("source", parallel_config=ParallelConfig(split_path="items"))
    branch_node = _make_agent_node("branch")
    final_node = _make_agent_node("final")
    graph = _make_graph(
        [source_node, branch_node, final_node],
        [
            Edge(edge_id="e1", source_node_id="source", target_node_id="branch"),
            Edge(edge_id="e2", source_node_id="branch", target_node_id="final"),
        ],
    )
    orchestrator = _make_orchestrator(
        {"source": source_runner, "branch": branch_runner, "final": final_runner},
        sqlite_db,
    )

    run = Run(
        graph_version_ref="test-parallel@1",
        deployment_ref="dep",
        thread_id="",
        current_node_ids=[],
        pending_node_ids=["source"],
        metadata=orchestrator._initial_metadata(graph, {"value": 1}),
    )
    persisted = await repo.create(run)
    run_id_box["id"] = persisted.run_id
    persisted.status = RunStatus.RUNNING
    persisted.touch()
    persisted = await repo.put(persisted)
    await repo.write_checkpoint(persisted)

    result = await orchestrator._drive(graph, persisted)

    assert result.status is RunStatus.FAILED
    assert final_calls["n"] == 0  # fan-in guard stopped before 'final'
