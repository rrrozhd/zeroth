from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.agents import (
    AgentConfig,
    AgentProviderError,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    ToolAttachmentManifest,
)


class DemoInput(BaseModel):
    query: str


class DemoOutput(BaseModel):
    answer: str
    score: int


@pytest.mark.asyncio
async def test_agent_runner_executes_declared_tool_calls() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Use tools when needed.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        tool_attachments=[
            ToolAttachmentManifest(
                alias="search",
                executable_unit_ref="eu://search",
                permission_scope=("net:query",),
            )
        ],
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-1", "name": "search", "args": {"query": "hello"}}],
            ),
            ProviderResponse(content='{"answer":"done","score":2}'),
        ]
    )
    tool_calls: list[tuple[str, dict[str, object]]] = []

    async def tool_executor(binding, arguments):  # noqa: ANN001
        tool_calls.append((binding.alias, dict(arguments)))
        return {"results": ["doc-1"]}

    runner = AgentRunner(
        config,
        provider,
        tool_executor=tool_executor,
        granted_tool_permissions=["net:query"],
    )

    result = await runner.run({"query": "hello"})

    assert result.output_data == {"answer": "done", "score": 2}
    assert tool_calls == [("search", {"query": "hello"})]
    assert provider.requests and len(provider.requests) == 2
    assert result.audit_record["extra"]["tool_calls"][0]["tool"]["alias"] == "search"


@pytest.mark.asyncio
async def test_agent_runner_keeps_operation_outcome_on_tool_call_audit() -> None:
    from zeroth.runtime.orchestration.tool_executor import OperationAwareToolOutput

    config = AgentConfig(
        name="demo",
        instruction="Use tools when needed.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        tool_attachments=[
            ToolAttachmentManifest(alias="search", executable_unit_ref="eu://search")
        ],
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-1", "name": "search", "args": {"query": "hello"}}],
            ),
            ProviderResponse(content='{"answer":"done","score":2}'),
        ]
    )

    async def tool_executor(_binding, _arguments):  # noqa: ANN001
        return OperationAwareToolOutput(
            {"results": ["doc-1"]},
            {
                "operation_key": "op-1",
                "operation_target_ref": "eu://search",
                "operation_support": "at_least_once",
                "operation_state": "completed",
                "operation_first_execution": False,
                "operation_replay_suppressed": True,
                "operation_reconciliation_required": False,
                "operation_reconciliation_exhausted": False,
                "operation_residual_duplicate_risk": False,
            },
        )

    result = await AgentRunner(config, provider, tool_executor=tool_executor).run(
        {"query": "hello"}
    )

    audit = result.audit_record["extra"]["tool_calls"][0]
    assert audit["operation_key"] == "op-1"
    assert audit["operation_state"] == "completed"
    assert audit["operation_replay_suppressed"] is True


@pytest.mark.asyncio
async def test_agent_runner_sums_every_provider_tool_turn() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Use tools when needed.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        tool_attachments=[
            ToolAttachmentManifest(alias="search", executable_unit_ref="eu://search")
        ],
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-1", "name": "search", "args": {}}],
                cost_usd=0.1,
                cost_measurement=MeasurementState.MEASURED,
            ),
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-2", "name": "search", "args": {}}],
                cost_usd=0.2,
                cost_measurement=MeasurementState.MEASURED,
            ),
            ProviderResponse(
                content='{"answer":"done","score":2}',
                cost_usd=0.3,
                cost_measurement=MeasurementState.MEASURED,
            ),
        ]
    )

    result = await AgentRunner(
        config,
        provider,
        tool_executor=lambda *_args: {"results": []},
    ).run({"query": "hello"})

    assert result.audit_record["cost_usd"] == pytest.approx(0.6)
    assert result.audit_record["cost_measurement"] is MeasurementState.MEASURED


@pytest.mark.asyncio
async def test_agent_runner_rejects_undeclared_tool_calls() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Use tools when needed.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        tool_attachments=[],
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-1", "name": "search", "args": {"query": "hello"}}],
            )
        ]
    )

    runner = AgentRunner(
        config,
        provider,
        tool_executor=lambda *_args, **_kwargs: {"results": []},
    )

    with pytest.raises(AgentProviderError, match="undeclared tool"):
        await runner.run({"query": "hello"})
