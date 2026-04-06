"""Unit tests for LiteLLMProviderAdapter.

All tests mock ChatLiteLLM to avoid network calls (per D-12).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.agent_runtime.provider import (
    LiteLLMProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)


@pytest.fixture
def adapter():
    return LiteLLMProviderAdapter()


def _make_ai_message(content="Hello", input_tokens=10, output_tokens=5):
    """Create a mock AIMessage with usage_metadata."""
    msg = MagicMock()
    msg.content = content
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    msg.tool_calls = []
    msg.response_metadata = {}
    return msg


async def test_ainvoke_returns_content(adapter):
    """LiteLLMProviderAdapter returns provider response with content."""
    mock_msg = _make_ai_message(content="Hi there")
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(model_name="openai/gpt-4o", messages=[])
        response = await adapter.ainvoke(request)
    assert response.content == "Hi there"
    assert isinstance(response, ProviderResponse)


async def test_ainvoke_extracts_token_usage(adapter):
    """Token usage is extracted from AIMessage.usage_metadata."""
    mock_msg = _make_ai_message(input_tokens=100, output_tokens=50)
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(
            model_name="anthropic/claude-sonnet-4-5-20250514", messages=[]
        )
        response = await adapter.ainvoke(request)
    assert response.token_usage is not None
    assert response.token_usage.input_tokens == 100
    assert response.token_usage.output_tokens == 50
    assert response.token_usage.total_tokens == 150
    assert response.token_usage.model_name == "anthropic/claude-sonnet-4-5-20250514"


async def test_ainvoke_handles_no_usage_metadata(adapter):
    """Token usage is None when AIMessage has no usage data."""
    mock_msg = MagicMock()
    mock_msg.content = "response"
    mock_msg.usage_metadata = None
    mock_msg.response_metadata = {}
    mock_msg.tool_calls = []
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(model_name="openai/gpt-4o", messages=[])
        response = await adapter.ainvoke(request)
    assert response.token_usage is None


async def test_ainvoke_converts_prompt_messages(adapter):
    """PromptMessage objects are converted to LangChain message types."""
    from zeroth.agent_runtime.models import PromptMessage

    mock_msg = _make_ai_message()
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(
            model_name="openai/gpt-4o",
            messages=[
                PromptMessage(role="system", content="You are helpful"),
                PromptMessage(role="user", content="Hello"),
            ],
        )
        await adapter.ainvoke(request)
    # Verify the client received LangChain messages
    call_args = mock_client.ainvoke.call_args[0][0]
    assert len(call_args) == 2
    from langchain_core.messages import HumanMessage, SystemMessage

    assert isinstance(call_args[0], SystemMessage)
    assert isinstance(call_args[1], HumanMessage)


async def test_ainvoke_metadata_includes_provider(adapter):
    """Response metadata includes provider and model info."""
    mock_msg = _make_ai_message()
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(model_name="openai/gpt-4o", messages=[])
        response = await adapter.ainvoke(request)
    assert response.metadata["provider"] == "litellm"
    assert response.metadata["model"] == "openai/gpt-4o"


async def test_ainvoke_fallback_response_metadata(adapter):
    """Token usage extracted from response_metadata when usage_metadata is absent."""
    mock_msg = MagicMock()
    mock_msg.content = "test"
    mock_msg.usage_metadata = None
    mock_msg.response_metadata = {
        "token_usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        }
    }
    mock_msg.tool_calls = []
    with patch.object(adapter, "_get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.ainvoke.return_value = mock_msg
        mock_get.return_value = mock_client
        request = ProviderRequest(model_name="openai/gpt-4o", messages=[])
        response = await adapter.ainvoke(request)
    assert response.token_usage is not None
    assert response.token_usage.input_tokens == 20
    assert response.token_usage.output_tokens == 10
    assert response.token_usage.total_tokens == 30
