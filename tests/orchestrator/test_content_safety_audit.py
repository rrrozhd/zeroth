"""SAFE-03 end-to-end: a content-blocked agent run is persisted as a rejected audit.

Proves the full path: AgentRunner raises AgentContentBlockedError (carrying an
``audit_record``) -> RuntimeOrchestrator._record_failed_execution_audit writes a
``rejected`` NodeAuditRecord with the findings -> the run fails cleanly.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tests.conftest import content_capture
from zeroth.contracts.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import (
    AgentConfig,
    AgentRunner,
    ContentSafetyConfig,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import RunStatus


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_blocked_agent_output_persists_rejected_audit(sqlite_db) -> None:
    runner = AgentRunner(
        AgentConfig(
            name="agent",
            instruction="respond",
            model_name="governai:test",
            input_model=_In,
            output_model=_Out,
            content_safety=ContentSafetyConfig(enabled=True, mode="block"),
        ),
        DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ssn 123-45-6789"}')]),
    )
    graph = Graph(
        graph_id="graph-cs",
        name="content-safety",
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref="graph-cs:v1",
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                agent=AgentNodeData(instruction="start", model_provider="provider://start"),
            ),
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=content_capture(AuditRepository(sqlite_db)),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"start": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    # the run fails cleanly (not an uncaught error)
    assert run.status is RunStatus.FAILED

    # and a rejected audit record carries the structured content-safety finding
    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert len(audits) == 1
    rejected = audits[0]
    assert rejected.status == "rejected"
    assert rejected.node_id == "start"
    output_safety = rejected.execution_metadata["content_safety"]["output"]
    assert output_safety["blocked"] is True
    assert any(finding["category"] == "pii:ssn" for finding in output_safety["findings"])
