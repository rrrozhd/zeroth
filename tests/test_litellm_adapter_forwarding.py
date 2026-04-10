"""Tests for LiteLLMProviderAdapter forwarding new ProviderRequest fields as kwargs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime.models import ModelParams
from zeroth.core.agent_runtime.provider import LiteLLMProviderAdapter, ProviderRequest


@pytest.fixture
def adapter() -> LiteLLMProviderAdapter:
    return LiteLLMProviderAdapter()


def _mock_ai_message(content: str = "ok") -> MagicMock:
    """Create a mock AIMessage with the expected attributes."""
    msg = MagicMock()
    msg.content = content
    msg.usage_metadata = None
    msg.response_metadata = {}
    msg.tool_calls = []
    return msg


class TestLiteLLMAdapterForwarding:
    @pytest.mark.asyncio
    async def test_tools_forwarded(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        tools = [{"type": "function", "function": {"name": "f"}}]
        request = ProviderRequest(model_name="openai/gpt-4o", tools=tools)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        _, kwargs = mock_client.ainvoke.call_args
        assert kwargs["tools"] == tools

    @pytest.mark.asyncio
    async def test_tool_choice_forwarded(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        request = ProviderRequest(model_name="openai/gpt-4o", tool_choice="auto")

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        _, kwargs = mock_client.ainvoke.call_args
        assert kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_response_format_forwarded_legacy(self, adapter: LiteLLMProviderAdapter) -> None:
        """Legacy response_format dict still forwarded when no output_model is set."""
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        fmt = {"type": "json_schema", "json_schema": {"name": "T", "schema": {}}}
        request = ProviderRequest(model_name="openai/gpt-4o", response_format=fmt)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        _, kwargs = mock_client.ainvoke.call_args
        assert kwargs["response_format"] == fmt

    @pytest.mark.asyncio
    async def test_output_model_uses_with_structured_output(self, adapter: LiteLLMProviderAdapter) -> None:
        """When output_model is set, adapter uses with_structured_output()."""

        class TestOutput(BaseModel):
            answer: str

        mock_msg = _mock_ai_message()
        parsed = TestOutput(answer="Paris")

        # Mock the structured output chain
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value={"raw": mock_msg, "parsed": parsed})

        mock_client = MagicMock()
        mock_client.with_structured_output = MagicMock(return_value=mock_structured)

        request = ProviderRequest(model_name="openai/gpt-4o", output_model=TestOutput)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            response = await adapter.ainvoke(request)

        # Verify with_structured_output was called with the model and include_raw=True
        mock_client.with_structured_output.assert_called_once_with(TestOutput, include_raw=True)
        mock_structured.ainvoke.assert_called_once()
        # Response content should be the parsed Pydantic instance
        assert isinstance(response.content, TestOutput)
        assert response.content.answer == "Paris"

    @pytest.mark.asyncio
    async def test_model_params_temperature_max_tokens(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        request = ProviderRequest(
            model_name="openai/gpt-4o",
            model_params=ModelParams(temperature=0.7, max_tokens=100),
        )

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        _, kwargs = mock_client.ainvoke.call_args
        assert kwargs["temperature"] == 0.7
        assert kwargs["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_model_params_top_p_stop_seed(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        request = ProviderRequest(
            model_name="openai/gpt-4o",
            model_params=ModelParams(top_p=0.9, stop=["END"], seed=42),
        )

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        _, kwargs = mock_client.ainvoke.call_args
        assert kwargs["top_p"] == 0.9
        assert kwargs["stop"] == ["END"]
        assert kwargs["seed"] == 42

    @pytest.mark.asyncio
    async def test_no_new_fields_no_extra_kwargs(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        request = ProviderRequest(model_name="openai/gpt-4o")

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        args, kwargs = mock_client.ainvoke.call_args
        # Should not have any of the new kwargs
        for key in ("tools", "tool_choice", "response_format", "temperature", "top_p", "max_tokens", "stop", "seed"):
            assert key not in kwargs, f"unexpected kwarg: {key}"

    @pytest.mark.asyncio
    async def test_empty_model_params_no_kwargs(self, adapter: LiteLLMProviderAdapter) -> None:
        mock_msg = _mock_ai_message()
        mock_client = MagicMock()
        mock_client.ainvoke = AsyncMock(return_value=mock_msg)

        request = ProviderRequest(
            model_name="openai/gpt-4o",
            model_params=ModelParams(),
        )

        with patch.object(adapter, "_get_client", return_value=mock_client):
            await adapter.ainvoke(request)

        args, kwargs = mock_client.ainvoke.call_args
        for key in ("temperature", "top_p", "max_tokens", "stop", "seed"):
            assert key not in kwargs, f"unexpected kwarg: {key}"
