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
from pydantic import BaseModel, ConfigDict, Field

from zeroth.agent_runtime.models import PromptMessage

ProviderMessage = PromptMessage | dict[str, Any] | Any


class ProviderRequest(BaseModel):
    """The request object sent to an AI model provider.

    Contains the model name, the list of messages to send, and any
    extra metadata the provider might need.
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str
    messages: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    """The response received from an AI model provider.

    Contains the text content, the raw provider-specific response,
    any tool calls the model wants to make, and extra metadata.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    content: Any = None
    raw: Any = None
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
