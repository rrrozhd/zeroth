"""Adapters for calling AI model providers.

A provider adapter is the bridge between the agent runtime and the actual
AI model (like an LLM). This module defines the interface that all adapters
must follow, plus several ready-made adapters for testing and production use.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM
from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.agent_runtime.models import ModelParams, PromptMessage
from zeroth.core.agent_runtime.response_format import build_response_format
from zeroth.core.governed.integrations.tool_calls import NormalizedToolCall, extract_tool_calls
from zeroth.governance.audit.models import TokenUsage
from zeroth.platform.secrets import SecretResolutionError, resolve_secret_async

if TYPE_CHECKING:
    from zeroth.platform.secrets import SecretProvider

ProviderMessage = PromptMessage | dict[str, Any] | Any

# LiteLLM provider prefix -> the ChatLiteLLM constructor field that pins that
# provider's key. ChatLiteLLM copies each of these onto the litellm client at
# call time (``_client_params``), and the constructor value wins over the env
# var the field's validator would otherwise read. Providers absent here still
# get the generic ``api_key`` (which sets ``litellm.api_key``).
_PROVIDER_KEY_FIELD: dict[str, str] = {
    "openai": "openai_api_key",
    "azure": "azure_api_key",
    "anthropic": "anthropic_api_key",
    "replicate": "replicate_api_key",
    "cohere": "cohere_api_key",
    "openrouter": "openrouter_api_key",
}


def _provider_prefix(model: str) -> str:
    """Derive the LiteLLM provider prefix from a model string.

    ``'openai/gpt-4o'`` -> ``'openai'``. Bare model names are mapped by a small
    heuristic (``gpt*`` -> openai, ``claude*`` -> anthropic); anything else
    falls back to the model string itself so a logical name is still produced.
    """
    if "/" in model:
        return model.split("/", 1)[0].lower()
    lowered = model.lower()
    if lowered.startswith("gpt") or lowered.startswith("o1") or lowered.startswith("o3"):
        return "openai"
    if lowered.startswith("claude"):
        return "anthropic"
    return lowered


def _key_fingerprint(key: str | None) -> str:
    """Short, non-reversible tag for a key used only in cache keys and logs."""
    if key is None:
        return "env"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


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


class LiteLLMProviderAdapter:
    """Universal LLM adapter using LangChain's ChatLiteLLM wrapper.

    Routes to any LiteLLM-supported provider (OpenAI, Anthropic, 100+ others)
    based on the model string in ProviderRequest.model_name.
    Uses LangChain interface per D-01 for governed-runtime compatibility.

    Model strings use LiteLLM format: ``openai/gpt-4o``,
    ``anthropic/claude-sonnet-4-5-20250514``, etc.

    Secret isolation (WS-F)
    -----------------------
    When a :class:`SecretProvider` is supplied, the api_key for each model's
    provider is resolved through it (scoped to ``tenant_id``) and injected into
    the ``ChatLiteLLM`` constructor, so the key never comes from a
    process-global environment variable. Clients are cached by
    ``(model, tenant_id, key_fingerprint)`` so a different tenant or a rotated
    key never reuses a stale client. With ``allow_env_fallback=False`` a missing
    key raises :class:`SecretResolutionError` instead of letting LiteLLM read
    the ambient env. When no provider is supplied (or fallback is allowed and
    the key is missing), LiteLLM's own env resolution is used, preserving the
    original behaviour.
    """

    def __init__(
        self,
        *,
        default_timeout: float = 600.0,
        secret_provider: SecretProvider | None = None,
        tenant_id: str | None = None,
        allow_env_fallback: bool = True,
        llm_key_map: dict[str, str] | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._secret_provider = secret_provider
        self._tenant_id = tenant_id
        self._allow_env_fallback = allow_env_fallback
        self._llm_key_map = dict(llm_key_map or {})
        # Cache key is (model, tenant_id, key_fingerprint) — never the raw key.
        self._clients: dict[tuple[str, str | None, str], ChatLiteLLM] = {}

    def _logical_name(self, model: str) -> str:
        """Map a model string to its logical secret name (e.g. ``llm.openai``)."""
        provider = _provider_prefix(model)
        if provider in self._llm_key_map:
            return self._llm_key_map[provider]
        return f"llm.{provider}"

    def _check_fail_closed(self, model: str, key: str | None) -> None:
        """Raise :class:`SecretResolutionError` when *key* is unresolved and fallback is off."""
        if key is None and not self._allow_env_fallback:
            # Fail closed: do NOT let LiteLLM silently read process env.
            raise SecretResolutionError(
                f"no secret for {self._logical_name(model)!r} "
                f"(tenant={self._tenant_id!r}) and env fallback is disabled"
            )

    def _resolve_api_key(self, model: str) -> str | None:
        """Resolve the api_key for *model* via the secret provider (fail-closed aware)."""
        if self._secret_provider is None:
            return None
        key = self._secret_provider.resolve_secret(
            self._logical_name(model), tenant_id=self._tenant_id
        )
        self._check_fail_closed(model, key)
        return key

    async def _resolve_api_key_async(self, model: str) -> str | None:
        """Async variant of :meth:`_resolve_api_key` for event-loop callers.

        A Vault-backed provider performs HTTP on a cache miss; resolving through
        the async helper keeps that off the event loop instead of stalling every
        concurrent run for the duration of the fetch.
        """
        if self._secret_provider is None:
            return None
        key = await resolve_secret_async(
            self._secret_provider, self._logical_name(model), tenant_id=self._tenant_id
        )
        self._check_fail_closed(model, key)
        return key

    def _get_client(self, model: str) -> ChatLiteLLM:
        """Get or create a ChatLiteLLM client for *model*, with an injected key."""
        return self._client_for(model, self._resolve_api_key(model))

    async def _get_client_async(self, model: str) -> ChatLiteLLM:
        """Get or create a client with the key resolved off the event loop."""
        return self._client_for(model, await self._resolve_api_key_async(model))

    def _client_for(self, model: str, api_key: str | None) -> ChatLiteLLM:
        """Build or reuse the client cached under (model, tenant, key fingerprint)."""
        cache_key = (model, self._tenant_id, _key_fingerprint(api_key))
        if cache_key not in self._clients:
            kwargs: dict[str, Any] = {"model": model, "timeout": self._default_timeout}
            if api_key is not None:
                # Set the generic key (pins ``litellm.api_key``) AND the
                # per-provider named field, which is the one that otherwise
                # retains the env value read by its validator at construction.
                kwargs["api_key"] = api_key
                named_field = _PROVIDER_KEY_FIELD.get(_provider_prefix(model))
                if named_field is not None:
                    kwargs[named_field] = api_key
            self._clients[cache_key] = ChatLiteLLM(**kwargs)
        return self._clients[cache_key]

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send request to LLM via ChatLiteLLM and return normalized response.

        When ``request.output_model`` is set, uses LangChain's
        ``with_structured_output()`` for provider-agnostic structured output.
        This handles schema generation, provider-specific formatting, and
        response parsing automatically, returning a typed Pydantic instance.
        """
        client = await self._get_client_async(request.model_name)
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

        response_format = (
            build_response_format(request.output_model)
            if request.output_model is not None
            else None
        )
        if response_format is not None:
            # Bind an OpenAI strict-compliant json_schema ourselves rather than
            # delegating to with_structured_output: langchain_litellm derives the
            # schema from the model and only patches additionalProperties, leaving
            # defaulted fields out of `required`, which OpenAI rejects under strict
            # mode. build_response_format enforces all strict rules. We then parse
            # the JSON content back into the Pydantic model.
            bound = client.bind(response_format=response_format)
            ai_message: AIMessage = await bound.ainvoke(lc_messages, **kwargs)
            token_usage = self._extract_token_usage(ai_message, request.model_name)
            tool_calls = self._extract_tool_calls(ai_message)
            if tool_calls:
                # A tool-call turn has no final content to parse — the agent
                # runner executes the tools and re-invokes; only the closing
                # response carries the structured output.
                return ProviderResponse(
                    content=ai_message.content or None,
                    raw=ai_message,
                    tool_calls=tool_calls,
                    token_usage=token_usage,
                    metadata={"provider": "litellm", "model": request.model_name},
                )
            parsed: BaseModel = request.output_model.model_validate_json(ai_message.content)
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
