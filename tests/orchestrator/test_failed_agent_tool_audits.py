"""Tool calls remain auditable when an agent fails after executing them."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tests.conftest import content_capture
from zeroth.contracts.graph import AgentNode, AgentNodeData, Graph
from zeroth.governance.audit import AuditRepository, AuditTimelineAssembler
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import (
    AgentConfig,
    AgentRunner,
    ContentSafetyConfig,
    DeterministicProviderAdapter,
    ProviderResponse,
    RetryPolicy,
    ToolAttachmentManifest,
)
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import RunStatus

pytestmark = pytest.mark.asyncio


class _Input(BaseModel):
    value: int


class _Output(BaseModel):
    answer: str


def _tool_call(call_id: str, password: str) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=[
            {
                "id": call_id,
                "name": "lookup",
                "args": {"password": password},
            }
        ]
    )


def _graph(graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name=graph_id,
        entry_step="agent",
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref=f"{graph_id}:v1",
                agent=AgentNodeData(instruction="test", model_provider="provider://test"),
            )
        ],
        edges=[],
    )


async def _failed_audit(
    sqlite_db,
    *,
    graph_id: str,
    responses: list[ProviderResponse | Exception],
    retry_policy: RetryPolicy | None = None,
    content_safety: ContentSafetyConfig | None = None,
):
    async def execute_tool(_binding, arguments):  # noqa: ANN001
        return {"token": f"result-for-{arguments['password']}"}

    runner = AgentRunner(
        AgentConfig(
            name="agent",
            instruction="test",
            model_name="governai:test",
            input_model=_Input,
            output_model=_Output,
            tool_attachments=[
                ToolAttachmentManifest(alias="lookup", executable_unit_ref="eu://lookup")
            ],
            retry_policy=retry_policy or RetryPolicy(),
            content_safety=content_safety or ContentSafetyConfig(),
        ),
        DeterministicProviderAdapter(responses),
        tool_executor=execute_tool,
    )
    repository = content_capture(AuditRepository.for_default_compatibility(sqlite_db))
    run = await RuntimeOrchestrator(
        audit_repository=repository,
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        agent_runners={"agent": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).run_graph(_graph(graph_id), {"value": 1})
    records = await repository.list_by_run(run.run_id)
    timeline = AuditTimelineAssembler().assemble(records)
    return run, records, timeline.entries


@pytest.mark.parametrize(
    ("graph_id", "final_response", "content_safety", "expected_status"),
    [
        (
            "failed-tool-validation",
            ProviderResponse(content={"wrong": "shape"}),
            None,
            "failed",
        ),
        (
            "failed-tool-content",
            ProviderResponse(content={"answer": "ssn 123-45-6789"}),
            ContentSafetyConfig(enabled=True, mode="block"),
            "rejected",
        ),
    ],
)
async def test_failed_agent_tool_call_is_redacted_and_retrievable_from_timeline(
    sqlite_db,
    graph_id: str,
    final_response: ProviderResponse,
    content_safety: ContentSafetyConfig | None,
    expected_status: str,
) -> None:
    run, records, entries = await _failed_audit(
        sqlite_db,
        graph_id=graph_id,
        responses=[_tool_call("tool-1", "input-secret"), final_response],
        content_safety=content_safety,
    )

    assert run.status is RunStatus.FAILED
    assert len(records) == len(entries) == 1
    assert records[0].status == expected_status
    for record in (records[0], entries[0]):
        assert len(record.tool_calls) == 1
        assert record.tool_calls[0].tool_ref == "eu://lookup"
        assert record.tool_calls[0].arguments == {"password": "***REDACTED***"}
        assert record.tool_calls[0].outcome == {"token": "***REDACTED***"}


async def test_later_provider_failure_keeps_tool_calls_from_validation_retry(sqlite_db) -> None:
    run, records, entries = await _failed_audit(
        sqlite_db,
        graph_id="failed-tool-retry",
        responses=[
            _tool_call("tool-1", "first-secret"),
            ProviderResponse(content={"wrong": "shape"}),
            _tool_call("tool-2", "second-secret"),
            RuntimeError("provider failed after the second tool"),
        ],
        retry_policy=RetryPolicy(max_retries=1, use_exponential_backoff=False),
    )

    assert run.status is RunStatus.FAILED
    assert len(records) == len(entries) == 1
    for record in (records[0], entries[0]):
        assert [call.arguments for call in record.tool_calls] == [
            {"password": "***REDACTED***"},
            {"password": "***REDACTED***"},
        ]
