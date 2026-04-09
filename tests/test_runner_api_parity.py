"""Tests for AgentRunner wiring of tools, response_format, and model_params."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    ToolAttachmentManifest,
)
from zeroth.agent_runtime.models import ModelParams
from zeroth.agent_runtime.response_format import build_response_format


# -- Fixtures -----------------------------------------------------------------


class SimpleInput(BaseModel):
    text: str


class SimpleOutput(BaseModel):
    result: str


class StructuredOutput(BaseModel):
    name: str
    score: int


_SENTINEL_USE_SIMPLE = object()


def _make_config(
    *,
    tool_attachments: list[ToolAttachmentManifest] | None = None,
    output_model: type[BaseModel] | object = _SENTINEL_USE_SIMPLE,
    model_params: ModelParams | None = None,
) -> AgentConfig:
    resolved_output = SimpleOutput if output_model is _SENTINEL_USE_SIMPLE else output_model
    return AgentConfig(
        name="test-agent",
        instruction="Test instruction.",
        model_name="test-model",
        input_model=SimpleInput,
        output_model=resolved_output,  # type: ignore[arg-type]
        tool_attachments=tool_attachments or [],
        model_params=model_params,
    )


# -- build_response_format unit tests ----------------------------------------


class TestBuildResponseFormat:
    def test_returns_none_for_bare_base_model(self) -> None:
        assert build_response_format(BaseModel) is None

    def test_returns_none_for_empty_model(self) -> None:
        class Empty(BaseModel):
            pass

        assert build_response_format(Empty) is None

    def test_returns_json_schema_for_custom_model(self) -> None:
        result = build_response_format(StructuredOutput)
        assert result is not None
        assert result["type"] == "json_schema"
        assert result["json_schema"]["name"] == "StructuredOutput"
        assert result["json_schema"]["strict"] is True
        assert "properties" in result["json_schema"]["schema"]


# -- Runner wiring tests -----------------------------------------------------


@pytest.mark.asyncio
async def test_runner_threads_tool_attachments_to_provider_request() -> None:
    """AgentRunner with tool_attachments produces ProviderRequest with tools field."""
    attachments = [
        ToolAttachmentManifest(
            alias="search",
            executable_unit_ref="eu://search",
            description="Search the web",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]
    config = _make_config(tool_attachments=attachments)
    provider = DeterministicProviderAdapter(
        [ProviderResponse(content='{"result":"ok"}')]
    )
    runner = AgentRunner(config, provider)
    await runner.run(SimpleInput(text="hello"))

    assert len(provider.requests) == 1
    req = provider.requests[0]
    assert req.tools is not None
    assert len(req.tools) == 1
    tool = req.tools[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "search"
    assert tool["function"]["description"] == "Search the web"
    assert tool["function"]["parameters"]["properties"]["query"]["type"] == "string"


@pytest.mark.asyncio
async def test_runner_threads_output_model_from_config() -> None:
    """AgentRunner with custom output_model produces ProviderRequest with output_model set."""
    config = _make_config(output_model=StructuredOutput)
    provider = DeterministicProviderAdapter(
        [ProviderResponse(content='{"name":"test","score":42}')]
    )
    runner = AgentRunner(config, provider)
    await runner.run(SimpleInput(text="hello"))

    assert len(provider.requests) == 1
    req = provider.requests[0]
    assert req.output_model is StructuredOutput


@pytest.mark.asyncio
async def test_runner_threads_model_params() -> None:
    """AgentRunner with model_params produces ProviderRequest with model_params set."""
    params = ModelParams(temperature=0.5, max_tokens=100)
    config = _make_config(model_params=params)
    provider = DeterministicProviderAdapter(
        [ProviderResponse(content='{"result":"ok"}')]
    )
    runner = AgentRunner(config, provider)
    await runner.run(SimpleInput(text="hello"))

    assert len(provider.requests) == 1
    req = provider.requests[0]
    assert req.model_params is not None
    assert req.model_params.temperature == 0.5
    assert req.model_params.max_tokens == 100


@pytest.mark.asyncio
async def test_runner_backward_compat_no_tool_attachments_or_model_params() -> None:
    """AgentRunner with no tool_attachments or model_params has those fields as None."""
    config = _make_config(output_model=SimpleOutput)
    provider = DeterministicProviderAdapter(
        [ProviderResponse(content='{"result":"ok"}')]
    )
    runner = AgentRunner(config, provider)
    await runner.run(SimpleInput(text="hello"))

    assert len(provider.requests) == 1
    req = provider.requests[0]
    assert req.tools is None
    assert req.model_params is None
    # response_format is set because SimpleOutput has fields (correct behavior);
    # it would be None only for bare BaseModel (which can't be used as output_model)


@pytest.mark.asyncio
async def test_resolve_tool_calls_also_includes_new_fields() -> None:
    """_resolve_tool_calls re-invocation also includes tools, response_format, model_params."""
    attachments = [
        ToolAttachmentManifest(
            alias="lookup",
            executable_unit_ref="eu://lookup",
            description="Look up a value",
            parameters_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        ),
    ]
    params = ModelParams(temperature=0.7)
    config = _make_config(
        tool_attachments=attachments,
        output_model=StructuredOutput,
        model_params=params,
    )

    # First response triggers a tool call, second response is the final answer
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[
                    {"id": "tc-1", "name": "lookup", "args": {"key": "abc"}},
                ],
            ),
            ProviderResponse(content='{"name":"found","score":10}'),
        ]
    )

    async def tool_executor(binding, arguments):  # noqa: ANN001
        return {"value": "result"}

    runner = AgentRunner(
        config, provider, tool_executor=tool_executor, granted_tool_permissions=[]
    )
    await runner.run(SimpleInput(text="hello"))

    # Two requests should have been made: initial + re-invocation after tool call
    assert len(provider.requests) == 2

    # Both requests should have tools, output_model, and model_params
    for req in provider.requests:
        assert req.tools is not None
        assert len(req.tools) == 1
        assert req.tools[0]["function"]["name"] == "lookup"
        assert req.output_model is StructuredOutput
        assert req.model_params is not None
        assert req.model_params.temperature == 0.7
