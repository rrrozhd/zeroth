"""Integration tests: tracing spans through the real orchestrator and runner (OBS-01)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    ToolAttachmentManifest,
)
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, RunStatus


class _In(BaseModel):
    value: int = 0


class _Out(BaseModel):
    answer: str


def _by_name(spans):
    return {s.name: s for s in spans}


@pytest.mark.asyncio
async def test_orchestrator_emits_run_node_agent_span_tree(otel_spans, sqlite_db) -> None:
    runner = AgentRunner(
        AgentConfig(
            name="demo",
            instruction="x",
            model_name="governai:test",
            input_model=_In,
            output_model=_Out,
        ),
        DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ok"}')]),
    )
    graph = Graph(
        graph_id="g-trace",
        name="trace",
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref="g-trace:v1",
                input_contract_ref="c://in",
                output_contract_ref="c://out",
                agent=AgentNodeData(instruction="start", model_provider="p://start"),
            ),
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"start": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})
    assert run.status is RunStatus.COMPLETED

    spans = _by_name(otel_spans.get_finished_spans())
    assert {"zeroth.run", "zeroth.node", "zeroth.agent"} <= set(spans)
    # node nests under run; agent nests under node — one trace end to end
    assert spans["zeroth.node"].parent.span_id == spans["zeroth.run"].context.span_id
    assert spans["zeroth.agent"].parent.span_id == spans["zeroth.node"].context.span_id
    assert spans["zeroth.node"].attributes["zeroth.node_id"] == "start"
    assert spans["zeroth.agent"].attributes["zeroth.agent"] == "demo"
    assert len({s.context.trace_id for s in spans.values()}) == 1  # single trace


@pytest.mark.asyncio
async def test_runner_emits_tool_span_nested_under_agent(otel_spans) -> None:
    config = AgentConfig(
        name="demo",
        instruction="use tools",
        model_name="governai:test",
        input_model=_In,
        output_model=_Out,
        tool_attachments=[
            ToolAttachmentManifest(alias="search", executable_unit_ref="eu://search")
        ],
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None, tool_calls=[{"id": "t1", "name": "search", "args": {}}]
            ),
            ProviderResponse(content='{"answer":"done"}'),
        ]
    )

    async def tool_executor(binding, arguments):  # noqa: ANN001
        return {"results": ["doc-1"]}

    runner = AgentRunner(config, provider, tool_executor=tool_executor)
    await runner.run({"value": 1})

    spans = _by_name(otel_spans.get_finished_spans())
    assert "zeroth.agent" in spans
    assert "zeroth.tool" in spans
    assert spans["zeroth.tool"].parent.span_id == spans["zeroth.agent"].context.span_id
    assert spans["zeroth.tool"].attributes["zeroth.tool"] == "search"
