from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import (
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
