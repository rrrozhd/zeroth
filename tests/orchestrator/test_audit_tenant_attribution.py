"""Node audit records must carry the run's tenant and workspace.

Before the fix, all four orchestrator audit-write sites omitted
tenant_id/workspace_id, so every node execution was stamped with the model
default ("default") regardless of the tenant that submitted the run —
breaking tenant attribution in the audit trail, evidence bundles, and the
per-record visibility filter in the audit API.
"""

from __future__ import annotations

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
from zeroth.contracts.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import Run, RunRepository, RunStatus


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    answer: str


def _graph() -> Graph:
    return Graph(
        graph_id="graph-tenant",
        name="tenant",
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref="graph-tenant:v1",
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                agent=AgentNodeData(instruction="start", model_provider="provider://start"),
            ),
        ],
        edges=[],
    )


def _orchestrator(sqlite_db, provider_responses: list[ProviderResponse]) -> RuntimeOrchestrator:
    runner = AgentRunner(
        AgentConfig(
            name="agent",
            instruction="respond",
            model_name="governai:test",
            input_model=_In,
            output_model=_Out,
        ),
        DeterministicProviderAdapter(provider_responses),
    )
    return RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"start": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )


async def _drive_run_for_tenant(orchestrator: RuntimeOrchestrator, graph: Graph) -> Run:
    """Mimic the service path: the Run row carries the submitting tenant."""
    run = Run(
        graph_version_ref=f"{graph.graph_id}@1",
        deployment_ref="dep-tenant",
        thread_id="",
        current_node_ids=[],
        pending_node_ids=["start"],
        metadata=orchestrator._initial_metadata(graph, {"value": 1}),
        tenant_id="acme",
        workspace_id="ws-1",
    )
    persisted = await orchestrator.run_repository.create(run)
    persisted.status = RunStatus.RUNNING
    persisted.touch()
    persisted = await orchestrator.run_repository.put(persisted)
    await orchestrator.run_repository.write_checkpoint(persisted)
    return await orchestrator._drive(graph, persisted)


@pytest.mark.asyncio
async def test_success_audit_carries_run_tenant(sqlite_db) -> None:
    orchestrator = _orchestrator(sqlite_db, [ProviderResponse(content='{"answer":"ok"}')])

    run = await _drive_run_for_tenant(orchestrator, _graph())
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert audits, "expected at least one node audit record"
    for record in audits:
        assert record.tenant_id == "acme"
        assert record.workspace_id == "ws-1"


@pytest.mark.asyncio
async def test_failure_audit_carries_run_tenant(sqlite_db) -> None:
    orchestrator = _orchestrator(sqlite_db, [ProviderResponse(content='{"answer":"ok"}')])
    graph = _graph()

    run = await _drive_run_for_tenant(orchestrator, graph)

    node = graph.nodes[0]
    error = RuntimeError("boom")
    error.audit_record = {"input": {"value": 1}}  # gate: only errors carrying one are audited
    await orchestrator._record_failed_execution_audit(
        run, node, node.node_id, {"value": 1}, error
    )

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    rejected = [r for r in audits if r.status == "rejected"]
    assert rejected, "expected the failed-execution audit record"
    for record in rejected:
        assert record.tenant_id == "acme"
        assert record.workspace_id == "ws-1"
