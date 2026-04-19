"""Adapters for calling AI model providers.

A provider adapter is the bridge between the agent runtime and the actual
AI model (like an LLM). This module defines the interface that all adapters
must follow, plus several ready-made adapters for testing and production use.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from governai.integrations.llm import GovernedLLM
from governai.integrations.tool_calls import NormalizedToolCall, extract_tool_calls
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM
from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.agent_runtime.models import ModelParams, PromptMessage
from zeroth.core.audit.models import TokenUsage

ProviderMessage = PromptMessage | dict[str, Any] | Any


class ProviderRequest(BaseModel):
    """The request object sent to an AI model provider.

    Contains the model name, the list of messages to send, and any
    extra metadata the provider might need.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    model_name: str
    messages: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    output_model: type[BaseModel] | None = None
    model_params: ModelParams | None = None


class ProviderResponse(BaseModel):
    """The response received from an AI model provider.

    Contains the text content, the raw provider-specific response,
    any tool calls the model wants to make, and extra metadata.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    content: Any = None
    raw: Any = None
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    token_usage: TokenUsage | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    cost_event_id: str | None = None


class ProviderAdapter(Protocol):
    """The interface that all provider adapters must follow.

    Any class with an ``ainvoke`` method that takes a ProviderRequest and
    returns a ProviderResponse can be used as a provider adapter.
    """

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send a request to the AI model and return its response."""


class DeterministicProviderAdapter:
    """A fake provider adapter for tests that returns pre-set responses.

    You give it a list of responses when you create it, and each call
    to ainvoke pops the next one off the list. Useful for testing
    agent behavior without calling a real AI model.
    """

    def __init__(self, responses: Sequence[ProviderResponse | Any | Exception]):
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Return the next queued response, or raise if the queue is empty."""
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("no queued responses")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        if isinstance(next_item, ProviderResponse):
            return next_item
        return ProviderResponse(content=next_item, raw=next_item)


class GovernedLLMProviderAdapter:
    """Adapter that connects to a GovernAI-managed LLM.

    Wraps a GovernedLLM instance so it can be used by the agent runtime.
    Handles both sync and async invoke methods automatically.
    """

    def __init__(self, model: GovernedLLM | Any):
        self._model = model

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send messages to the GovernAI LLM and return a normalized response."""
        messages = [
            message.model_dump(mode="json") if hasattr(message, "model_dump") else message
            for message in request.messages
        ]
        invoker = getattr(self._model, "ainvoke", None)
        if not callable(invoker):
            invoker = getattr(self._model, "invoke", None)
        if not callable(invoker):
            raise RuntimeError("provider does not expose invoke/ainvoke")
        result = invoker(messages, model=request.model_name)
        if inspect.isawaitable(result):
            result = await result
        raw = result.raw if hasattr(result, "raw") else result
        content = getattr(result, "content", None)
        if content is None and hasattr(raw, "content"):
            content = raw.content
        if content is None and not hasattr(result, "tool_calls"):
            content = result
        tool_calls = list(getattr(result, "tool_calls", None) or extract_tool_calls(raw))
        return ProviderResponse(
            content=content,
            raw=raw,
            tool_calls=tool_calls,
            metadata={"provider": "governai"},
        )


class LiteLLMProviderAdapter:
    """Universal LLM adapter using LangChain's ChatLiteLLM wrapper.

    Routes to any LiteLLM-supported provider (OpenAI, Anthropic, 100+ others)
    based on the model string in ProviderRequest.model_name.
    Uses LangChain interface per D-01 for GovernAI compatibility.

    Model strings use LiteLLM format: ``openai/gpt-4o``,
    ``anthropic/claude-sonnet-4-5-20250514``, etc.  API keys are read from
    standard environment variables (``OPENAI_API_KEY``,
    ``ANTHROPIC_API_KEY``, ...) by LiteLLM automatically.
    """

    def __init__(self, *, default_timeout: float = 600.0) -> None:
        self._default_timeout = default_timeout
        self._clients: dict[str, ChatLiteLLM] = {}

    def _get_client(self, model: str) -> ChatLiteLLM:
        """Get or create a ChatLiteLLM client for the given model string."""
        if model not in self._clients:
            self._clients[model] = ChatLiteLLM(
                model=model,
                timeout=self._default_timeout,
            )
        return self._clients[model]

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send request to LLM via ChatLiteLLM and return normalized response.

        When ``request.output_model`` is set, uses LangChain's
        ``with_structured_output()`` for provider-agnostic structured output.
        This handles schema generation, provider-specific formatting, and
        response parsing automatically, returning a typed Pydantic instance.
        """
        client = self._get_client(request.model_name)
        lc_messages = self._to_langchain_messages(request.messages)
        kwargs: dict[str, Any] = {}
        if request.tools is not None:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice
        if request.model_params is not None:
            params = request.model_params
            if params.temperature is not None:
                kwargs["temperature"] = params.temperature
            if params.top_p is not None:
                kwargs["top_p"] = params.top_p
            if params.max_tokens is not None:
                kwargs["max_tokens"] = params.max_tokens
            if params.stop is not None:
                kwargs["stop"] = params.stop
            if params.seed is not None:
                kwargs["seed"] = params.seed

        if request.output_model is not None:
            # Use LangChain's with_structured_output for provider-agnostic
            # structured output. include_raw=True gives us the AIMessage
            # alongside the parsed Pydantic model for token usage extraction.
            structured = client.with_structured_output(
                request.output_model, include_raw=True
            )
            result = await structured.ainvoke(lc_messages, **kwargs)
            ai_message: AIMessage = result["raw"]
            parsed: BaseModel = result["parsed"]
            token_usage = self._extract_token_usage(ai_message, request.model_name)
            tool_calls = self._extract_tool_calls(ai_message)
            return ProviderResponse(
                content=parsed,
                raw=ai_message,
                tool_calls=tool_calls,
                token_usage=token_usage,
                metadata={"provider": "litellm", "model": request.model_name},
            )

        # Fallback: no structured output — plain invocation.
        # Supports legacy response_format dict if provided directly.
        if request.response_format is not None:
            kwargs["response_format"] = request.response_format
        ai_message = await client.ainvoke(lc_messages, **kwargs)
        token_usage = self._extract_token_usage(ai_message, request.model_name)
        tool_calls = self._extract_tool_calls(ai_message)
        return ProviderResponse(
            content=ai_message.content,
            raw=ai_message,
            tool_calls=tool_calls,
            token_usage=token_usage,
            metadata={"provider": "litellm", "model": request.model_name},
        )

    def _to_langchain_messages(self, messages: list[Any]) -> list[Any]:
        """Convert PromptMessage or dict messages to LangChain message objects."""
        result: list[Any] = []
        for msg in messages:
            if isinstance(msg, PromptMessage):
                role, content = msg.role, msg.content
            elif isinstance(msg, dict):
                role, content = msg.get("role", "user"), msg.get("content", "")
            else:
                result.append(msg)  # Already a LangChain message
                continue
            if role == "system":
                result.append(SystemMessage(content=content))
            elif role == "assistant":
                result.append(AIMessage(content=content))
            else:
                result.append(HumanMessage(content=content))
        return result

    def _extract_token_usage(self, ai_message: AIMessage, model_name: str) -> TokenUsage | None:
        """Extract token usage from AIMessage.usage_metadata or response_metadata."""
        # Try usage_metadata first (LangChain standard)
        usage_meta = getattr(ai_message, "usage_metadata", None)
        if usage_meta and isinstance(usage_meta, dict):
            input_t = usage_meta.get("input_tokens", 0)
            output_t = usage_meta.get("output_tokens", 0)
            total_t = usage_meta.get("total_tokens", input_t + output_t)
            return TokenUsage(
                input_tokens=input_t,
                output_tokens=output_t,
                total_tokens=total_t,
                model_name=model_name,
            )
        # Fallback: response_metadata.token_usage (OpenAI-style)
        resp_meta = getattr(ai_message, "response_metadata", None)
        if resp_meta and isinstance(resp_meta, dict):
            token_usage_dict = resp_meta.get("token_usage", {})
            if token_usage_dict:
                return TokenUsage(
                    input_tokens=token_usage_dict.get("prompt_tokens", 0),
                    output_tokens=token_usage_dict.get("completion_tokens", 0),
                    total_tokens=token_usage_dict.get("total_tokens", 0),
                    model_name=model_name,
                )
        return None

    def _extract_tool_calls(self, ai_message: AIMessage) -> list[NormalizedToolCall]:
        """Extract tool calls from AIMessage if present."""
        raw_tool_calls = getattr(ai_message, "tool_calls", None)
        if not raw_tool_calls:
            return []
        result = []
        for tc in raw_tool_calls:
            result.append(
                NormalizedToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    args=tc.get("args", {}),
                )
            )
        return result


class CallableProviderAdapter:
    """Wraps any function as a provider adapter.

    Pass in a regular function or an async function that takes a
    ProviderRequest and returns a response. This adapter will call it
    and wrap the result in a ProviderResponse if needed.
    """

    def __init__(self, func: Callable[[ProviderRequest], ProviderResponse | Any | Awaitable[Any]]):
        self._func = func

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Call the wrapped function and return a normalized response."""
        result = self._func(request)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ProviderResponse):
            return result
        return ProviderResponse(content=result, raw=result, tool_calls=extract_tool_calls(result))


async def run_provider_with_timeout(
    adapter: ProviderAdapter,
    request: ProviderRequest,
    *,
    timeout_seconds: float | None,
) -> ProviderResponse:
    """Call a provider adapter, cancelling the call if it takes too long.

    If timeout_seconds is None, no time limit is applied.
    """
    if timeout_seconds is None:
        return await adapter.ainvoke(request)
    return await asyncio.wait_for(adapter.ainvoke(request), timeout=timeout_seconds)
