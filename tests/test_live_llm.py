"""Live integration tests for LLM providers.

These tests call real provider APIs and are gated behind @pytest.mark.live.
They only run when the corresponding API keys are present in the environment.
Skip in CI: pytest -m "not live"
Run live: pytest -m live
"""

import os

import pytest

from zeroth.agent_runtime.provider import LiteLLMProviderAdapter, ProviderRequest

pytestmark = pytest.mark.live


def _has_openai_key():
    return bool(os.environ.get("OPENAI_API_KEY"))


def _has_anthropic_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.skipif(not _has_openai_key(), reason="OPENAI_API_KEY not set")
async def test_openai_live_call():
    """LLM-01: OpenAI provider works end-to-end with real API."""
    adapter = LiteLLMProviderAdapter()
    request = ProviderRequest(
        model_name="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "Reply with exactly: HELLO"}],
    )
    response = await adapter.ainvoke(request)
    assert response.content is not None
    assert len(response.content) > 0
    # Token usage should be present for OpenAI
    assert response.token_usage is not None
    assert response.token_usage.input_tokens > 0
    assert response.token_usage.output_tokens > 0
    assert response.token_usage.model_name == "openai/gpt-4o-mini"


@pytest.mark.skipif(not _has_anthropic_key(), reason="ANTHROPIC_API_KEY not set")
async def test_anthropic_live_call():
    """LLM-02: Anthropic provider works end-to-end with real API."""
    adapter = LiteLLMProviderAdapter()
    request = ProviderRequest(
        model_name="anthropic/claude-3-5-haiku-20241022",
        messages=[{"role": "user", "content": "Reply with exactly: HELLO"}],
    )
    response = await adapter.ainvoke(request)
    assert response.content is not None
    assert len(response.content) > 0
    # Token usage should be present for Anthropic
    assert response.token_usage is not None
    assert response.token_usage.input_tokens > 0
    assert response.token_usage.output_tokens > 0
    assert response.token_usage.model_name == "anthropic/claude-3-5-haiku-20241022"


@pytest.mark.skipif(not _has_openai_key(), reason="OPENAI_API_KEY not set")
async def test_token_usage_flows_to_response():
    """LLM-04: Token usage is populated on ProviderResponse."""
    adapter = LiteLLMProviderAdapter()
    request = ProviderRequest(
        model_name="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "Say hi"}],
    )
    response = await adapter.ainvoke(request)
    assert response.token_usage is not None
    assert response.token_usage.total_tokens == (
        response.token_usage.input_tokens + response.token_usage.output_tokens
    )
