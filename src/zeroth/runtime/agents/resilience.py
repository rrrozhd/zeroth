"""Provider resilience: model fallback chains and response caching (PRES).

Two composable ``ProviderAdapter`` wrappers (same pattern as
``econ.InstrumentedProviderAdapter``), so they drop in anywhere a provider is
expected and stack freely:

    provider = CachingProviderAdapter(
        FallbackProviderAdapter.across_models(
            LiteLLMProviderAdapter(),
            ["openai/gpt-4o", "anthropic/claude-sonnet-4-5"],
        )
    )

* ``FallbackProviderAdapter`` (PRES-01) tries an ordered list of targets and
  fails over to the next when a call raises a *transient* provider error
  (rate limit, 5xx, timeout, connection — reusing the runtime's retry
  classifier). Deterministic errors (bad request, auth) are not retried across
  targets since an alternate would fail identically.
* ``CachingProviderAdapter`` (PRES-02) is an **exact-match** cache keyed on the
  full request (model + messages + params + tools + schema). It is not a
  semantic cache; the ``ResponseCache`` protocol leaves room for one.

Caching is outermost in the example above so a hit skips the whole fallback
chain. Both wrappers are opt-in — nothing is wrapped unless the caller does so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from cachetools import LRUCache, TTLCache

from zeroth.runtime.agents.provider import (
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)
from zeroth.runtime.agents.retry import is_retryable_provider_error


@dataclass(frozen=True)
class ProviderTarget:
    """One step in a fallback chain: an adapter, with an optional model override.

    When ``model_name`` is set, the request's model is rewritten to it before the
    adapter is called — the common case for falling over to a different model on
    the same (e.g. LiteLLM) adapter.
    """

    adapter: ProviderAdapter
    model_name: str | None = None


class FallbackProviderAdapter:
    """Tries an ordered list of provider targets, failing over on transient errors."""

    def __init__(
        self,
        targets: Sequence[ProviderTarget],
        *,
        should_fallback: Callable[[BaseException], bool] | None = None,
    ) -> None:
        target_list = list(targets)
        if not target_list:
            raise ValueError("FallbackProviderAdapter requires at least one target")
        self._targets = target_list
        # Default: fail over only on transient/availability errors. A deterministic
        # error (400, auth, content policy) would fail identically on every target.
        self._should_fallback = should_fallback or is_retryable_provider_error

    @classmethod
    def across_models(
        cls,
        adapter: ProviderAdapter,
        models: Sequence[str],
        *,
        should_fallback: Callable[[BaseException], bool] | None = None,
    ) -> FallbackProviderAdapter:
        """Build a chain over one adapter and an ordered list of model strings."""
        model_list = list(models)
        if not model_list:
            raise ValueError("across_models requires at least one model")
        return cls(
            [ProviderTarget(adapter, model) for model in model_list],
            should_fallback=should_fallback,
        )

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Invoke targets in order; return the first success, else raise the last error."""
        last_index = len(self._targets) - 1
        for index, target in enumerate(self._targets):
            call_request = (
                request
                if target.model_name is None
                else request.model_copy(update={"model_name": target.model_name})
            )
            try:
                response = await target.adapter.ainvoke(call_request)
            except Exception as exc:
                # Stop if this was the last target, or the error is not one a
                # different target could recover from.
                if index == last_index or not self._should_fallback(exc):
                    raise
                continue
            response.metadata["fallback"] = {
                "target_index": index,
                "model": call_request.model_name,
                "fell_back": index > 0,
            }
            return response
        raise RuntimeError("fallback chain exhausted without a result")  # pragma: no cover


class ResponseCache(Protocol):
    """A keyed store of provider responses (pluggable: in-memory, Redis, semantic, ...)."""

    def get(self, key: str) -> ProviderResponse | None:
        """Return the cached response for ``key``, or None on miss."""
        ...

    def set(self, key: str, response: ProviderResponse) -> None:
        """Store ``response`` under ``key``."""
        ...


class InMemoryResponseCache:
    """Process-local response cache backed by cachetools (LRU, or TTL when set).

    Intended for single-process use; not shared across workers. Concurrent
    fan-out branches may double-compute a cold key (benign), never corrupt it.
    """

    def __init__(self, *, maxsize: int = 1024, ttl: float | None = None) -> None:
        self._cache: Any = (
            TTLCache(maxsize=maxsize, ttl=ttl) if ttl is not None else LRUCache(maxsize=maxsize)
        )

    def get(self, key: str) -> ProviderResponse | None:
        """Return the cached response for ``key``, or None on miss/expiry."""
        return self._cache.get(key)

    def set(self, key: str, response: ProviderResponse) -> None:
        """Store ``response`` under ``key`` (evicting per the LRU/TTL policy)."""
        self._cache[key] = response


def _message_repr(message: Any) -> Any:
    """Best-effort, stable representation of a message for cache keying."""
    if hasattr(message, "model_dump"):
        try:
            return message.model_dump(mode="json")
        except Exception:  # pragma: no cover - exotic message types
            return repr(message)
    if isinstance(message, dict):
        return message
    role = getattr(message, "role", None)
    content = getattr(message, "content", None)
    if role is not None or content is not None:
        return {"role": role, "content": content}
    return repr(message)


class CachingProviderAdapter:
    """Exact-match response cache over any ``ProviderAdapter`` (PRES-02).

    The key covers everything that changes a response: model, messages, model
    params, tools, tool choice, and the output schema. Tool-call responses are
    not cached by default (replaying a stale tool decision is unsafe).
    """

    def __init__(
        self,
        inner: ProviderAdapter,
        cache: ResponseCache | None = None,
        *,
        cache_tool_calls: bool = False,
    ) -> None:
        self._inner = inner
        self._cache: ResponseCache = cache if cache is not None else InMemoryResponseCache()
        self._cache_tool_calls = cache_tool_calls

    def _cache_key(self, request: ProviderRequest) -> str:
        payload = {
            "model_name": request.model_name,
            "messages": [_message_repr(m) for m in request.messages],
            "model_params": (
                request.model_params.model_dump() if request.model_params is not None else None
            ),
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
            "output_model": (
                request.output_model.__name__ if request.output_model is not None else None
            ),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Return a cached response on an exact match, else call the inner adapter."""
        key = self._cache_key(request)
        cached = self._cache.get(key)
        if cached is not None:
            return cached.model_copy(update={"metadata": {**cached.metadata, "cache_hit": True}})
        response = await self._inner.ainvoke(request)
        if self._cache_tool_calls or not response.tool_calls:
            self._cache.set(key, response)
        return response.model_copy(update={"metadata": {**response.metadata, "cache_hit": False}})
