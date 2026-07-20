"""OBS-02: fan-out branch spans nest under the fan-out span (real orchestrator path).

Trace context must cross the asyncio tasks the parallel executor spawns, so each
branch's node span is a child of the ``zeroth.fanout`` span in the same trace.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import AgentNode, AgentNodeData, Edge, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.runtime.parallel.models import ParallelConfig
from zeroth.core.runs import RunRepository, RunStatus


class ItemsInput(BaseModel):
    value: int = 0


class BranchItemInput(BaseModel):
    x: int = 0


class ItemsOutput(BaseModel):
    items: list[dict[str, Any]] = []


class ProcessedOutput(BaseModel):
    result: int = 0


@pytest.mark.asyncio
async def test_fanout_branch_node_spans_nest_under_fanout_span(otel_spans, sqlite_db) -> None:
    source_runner = AgentRunner(
        AgentConfig(
            name="source",
            instruction="t",
            model_name="governai:test",
            input_model=ItemsInput,
            output_model=ItemsOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]})
        ),
    )
    sink_runner = AgentRunner(
        AgentConfig(
            name="sink",
            instruction="t",
            model_name="governai:test",
            input_model=BranchItemInput,
            output_model=ProcessedOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(
                content={"result": req.metadata["input_payload"].get("x", 0) * 10}
            )
        ),
    )
    graph = Graph(
        graph_id="g-fanout",
        name="fanout",
        entry_step="source",
        execution_settings=ExecutionSettings(max_total_steps=50),
        nodes=[
            AgentNode(
                node_id="source",
                graph_version_ref="g-fanout:v1",
                agent=AgentNodeData(instruction="t", model_provider="p://source"),
                parallel_config=ParallelConfig(split_path="items"),
            ),
            AgentNode(
                node_id="sink",
                graph_version_ref="g-fanout:v1",
                agent=AgentNodeData(instruction="t", model_provider="p://sink"),
            ),
        ],
        edges=[Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"source": source_runner, "sink": sink_runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})
    assert run.status is RunStatus.COMPLETED

    spans = otel_spans.get_finished_spans()
    fanout = next(s for s in spans if s.name == "zeroth.fanout")
    # the downstream "sink" node runs once per branch; those node spans must be
    # children of the fan-out span (proves trace context crossed the asyncio tasks)
    sink_node_spans = [
        s
        for s in spans
        if s.name == "zeroth.node" and s.attributes.get("zeroth.node_id") == "sink"
    ]
    assert len(sink_node_spans) == 2
    for span in sink_node_spans:
        assert span.parent is not None
        assert span.parent.span_id == fanout.context.span_id
        assert span.context.trace_id == fanout.context.trace_id
