"""Node audit records must report a real wall-clock duration.

Before the fix, completed_at was a call-time now() evaluated before started_at's
default factory, so the clamp validator forced completed_at == started_at and
every record showed a zero duration. started_at is now the node's dispatch time,
threaded from the run loop, so the delta reflects actual execution time.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.audit import AuditRepository
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.core.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, RunStatus


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_node_audit_records_a_real_duration(sqlite_db) -> None:
    runner = AgentRunner(
        AgentConfig(
            name="agent",
            instruction="respond",
            model_name="governai:test",
            input_model=_In,
            output_model=_Out,
        ),
        DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ok"}')]),
    )
    # Make node execution take measurable time: a construction-time started_at
    # (the old zero-duration bug) would collapse the delta to ~0, whereas the
    # dispatch-time started_at yields >= the sleep.
    original_run = runner.run

    async def slow_run(*args, **kwargs):
        await asyncio.sleep(0.02)
        return await original_run(*args, **kwargs)

    runner.run = slow_run

    graph = Graph(
        graph_id="graph-dur",
        name="dur",
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref="graph-dur:v1",
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                agent=AgentNodeData(instruction="start", model_provider="provider://start"),
            ),
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"start": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert len(audits) == 1
    record = audits[0]
    duration = (record.completed_at - record.started_at).total_seconds()
    assert duration >= 0.01, f"expected a real duration, got {duration}s"
