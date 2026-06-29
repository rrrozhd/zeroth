"""Tests for provider resilience: fallback chains and response caching (PRES)."""

from __future__ import annotations

import pytest

from zeroth.core.agent_runtime import (
    CachingProviderAdapter,
    DeterministicProviderAdapter,
    FallbackProviderAdapter,
    InMemoryResponseCache,
    ModelParams,
    ProviderTarget,
)
from zeroth.core.agent_runtime.provider import ProviderRequest, ProviderResponse


def _req(model: str = "openai/gpt-4o", **kwargs) -> ProviderRequest:
    return ProviderRequest(model_name=model, messages=[{"role": "user", "content": "hi"}], **kwargs)


def _tool_response() -> ProviderResponse:
    return ProviderResponse(content=None, tool_calls=[{"id": "t1", "name": "search", "args": {}}])


# --- Fallback (PRES-01) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_returns_primary_on_success() -> None:
    primary = DeterministicProviderAdapter([ProviderResponse(content="primary")])
    secondary = DeterministicProviderAdapter([ProviderResponse(content="secondary")])
    adapter = FallbackProviderAdapter(
        [ProviderTarget(primary), ProviderTarget(secondary)],
        should_fallback=lambda exc: True,
    )

    resp = await adapter.ainvoke(_req())

    assert resp.content == "primary"
    assert resp.metadata["fallback"] == {
        "target_index": 0,
        "model": "openai/gpt-4o",
        "fell_back": False,
    }
    assert secondary.requests == []  # the fallback was never reached


@pytest.mark.asyncio
async def test_fallback_uses_next_target_on_failure() -> None:
    primary = DeterministicProviderAdapter([RuntimeError("primary down")])
    secondary = DeterministicProviderAdapter([ProviderResponse(content="secondary")])
    adapter = FallbackProviderAdapter(
        [ProviderTarget(primary), ProviderTarget(secondary)],
        should_fallback=lambda exc: True,
    )

    resp = await adapter.ainvoke(_req())

    assert resp.content == "secondary"
    assert resp.metadata["fallback"]["fell_back"] is True
    assert resp.metadata["fallback"]["target_index"] == 1


@pytest.mark.asyncio
async def test_across_models_overrides_model_and_falls_over() -> None:
    # One adapter, two models: the request's model is rewritten per target.
    adapter = DeterministicProviderAdapter(
        [RuntimeError("model-a down"), ProviderResponse(content="ok")]
    )
    chain = FallbackProviderAdapter.across_models(
        adapter, ["model-a", "model-b"], should_fallback=lambda exc: True
    )

    resp = await chain.ainvoke(_req(model="orig"))

    assert resp.content == "ok"
    assert [r.model_name for r in adapter.requests] == ["model-a", "model-b"]
    assert resp.metadata["fallback"]["model"] == "model-b"


@pytest.mark.asyncio
async def test_fallback_raises_last_error_when_all_fail() -> None:
    primary = DeterministicProviderAdapter([RuntimeError("p")])
    secondary = DeterministicProviderAdapter([ValueError("s")])
    adapter = FallbackProviderAdapter(
        [ProviderTarget(primary), ProviderTarget(secondary)],
        should_fallback=lambda exc: True,
    )

    with pytest.raises(ValueError, match="s"):
        await adapter.ainvoke(_req())


@pytest.mark.asyncio
async def test_default_predicate_does_not_fall_over_on_non_transient() -> None:
    # Default predicate is is_retryable_provider_error; a plain ValueError is
    # permanent, so the chain must not waste an attempt on the alternate.
    primary = DeterministicProviderAdapter([ValueError("bad request")])
    secondary = DeterministicProviderAdapter([ProviderResponse(content="secondary")])
    adapter = FallbackProviderAdapter([ProviderTarget(primary), ProviderTarget(secondary)])

    with pytest.raises(ValueError, match="bad request"):
        await adapter.ainvoke(_req())
    assert secondary.requests == []


def test_fallback_requires_at_least_one_target() -> None:
    with pytest.raises(ValueError, match="at least one target"):
        FallbackProviderAdapter([])


# --- Caching (PRES-02) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_then_hit_calls_inner_once() -> None:
    inner = DeterministicProviderAdapter([ProviderResponse(content="answer")])
    cached = CachingProviderAdapter(inner)

    first = await cached.ainvoke(_req())
    second = await cached.ainvoke(_req())  # identical request

    assert first.content == "answer" and second.content == "answer"
    assert first.metadata["cache_hit"] is False
    assert second.metadata["cache_hit"] is True
    assert len(inner.requests) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_cache_key_varies_by_model_params() -> None:
    inner = DeterministicProviderAdapter(
        [ProviderResponse(content="a"), ProviderResponse(content="b")]
    )
    cached = CachingProviderAdapter(inner)

    await cached.ainvoke(_req(model_params=ModelParams(temperature=0.0)))
    await cached.ainvoke(_req(model_params=ModelParams(temperature=0.9)))

    assert len(inner.requests) == 2  # different params -> different key -> miss


@pytest.mark.asyncio
async def test_cache_skips_tool_call_responses_by_default() -> None:
    inner = DeterministicProviderAdapter([_tool_response(), _tool_response()])
    cached = CachingProviderAdapter(inner)

    await cached.ainvoke(_req())
    await cached.ainvoke(_req())

    assert len(inner.requests) == 2  # tool-call responses are not cached


@pytest.mark.asyncio
async def test_cache_tool_calls_when_enabled() -> None:
    inner = DeterministicProviderAdapter([_tool_response()])
    cached = CachingProviderAdapter(inner, cache_tool_calls=True)

    await cached.ainvoke(_req())
    second = await cached.ainvoke(_req())

    assert len(inner.requests) == 1
    assert second.metadata["cache_hit"] is True


def test_inmemory_cache_get_set_and_lru_eviction() -> None:
    cache = InMemoryResponseCache(maxsize=1)
    first = ProviderResponse(content="a")
    second = ProviderResponse(content="b")

    cache.set("k1", first)
    assert cache.get("k1") is first
    cache.set("k2", second)  # evicts k1 under LRU maxsize=1
    assert cache.get("k1") is None
    assert cache.get("k2") is second
