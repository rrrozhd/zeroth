"""Tests for budget enforcement: BudgetEnforcer and BudgetExceededError.

Covers under-budget, over-budget, TTL cache hits, fail-open on
Regulus unavailability, and BudgetExceededError attributes.
Also integration tests for AgentRunner with budget_enforcer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime.errors import BudgetExceededError
from zeroth.core.agent_runtime.models import AgentConfig
from zeroth.core.agent_runtime.provider import DeterministicProviderAdapter, ProviderResponse
from zeroth.core.agent_runtime.runner import AgentRunner
from zeroth.core.econ.budget import BudgetEnforcer


class _Input(BaseModel):
    query: str


class _Output(BaseModel):
    answer: str


def _mock_transport(*, json_data: dict, status_code: int = 200) -> httpx.MockTransport:
    """Return an httpx MockTransport that always returns the given JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, json=json_data)

    return handler


# -- Test 1: under budget --


@pytest.mark.asyncio
async def test_check_budget_under_budget():
    """BudgetEnforcer returns (True, 50.0, 100.0) when tenant is under budget."""
    transport = _mock_transport(json_data={"total_cost_usd": 50, "budget_cap_usd": 100})
    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=30,
        timeout=5.0,
        _transport=transport,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant-1")
    assert allowed is True
    assert spend == 50.0
    assert cap == 100.0


# -- Test 2: over budget --


@pytest.mark.asyncio
async def test_check_budget_over_budget():
    """BudgetEnforcer returns (False, 105.0, 100.0) when tenant exceeds budget."""
    transport = _mock_transport(json_data={"total_cost_usd": 105, "budget_cap_usd": 100})
    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=30,
        timeout=5.0,
        _transport=transport,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant-1")
    assert allowed is False
    assert spend == 105.0
    assert cap == 100.0


# -- Test 3: cache hit (no second HTTP request) --


@pytest.mark.asyncio
async def test_check_budget_cache_hit():
    """Second call within TTL returns cached result without HTTP request."""
    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"total_cost_usd": 20, "budget_cap_usd": 100})

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=60,
        timeout=5.0,
        _transport=counting_handler,
    )
    r1 = await enforcer.check_budget("tenant-1")
    r2 = await enforcer.check_budget("tenant-1")
    assert r1 == r2
    assert call_count == 1  # only one HTTP call


# -- Test 4: fail-open when Regulus unreachable --


@pytest.mark.asyncio
async def test_check_budget_fail_open_connection_error():
    """When Regulus is unreachable, execution is allowed (fail-open)."""

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=30,
        timeout=5.0,
        _transport=error_handler,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant-1")
    assert allowed is True
    assert spend == 0.0
    assert cap == float("inf")


@pytest.mark.asyncio
async def test_check_budget_fail_open_is_logged(caplog):
    """A silent fail-open hides that caps aren't enforced — it must warn."""
    import logging

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=30,
        timeout=5.0,
        _transport=error_handler,
    )
    with caplog.at_level(logging.WARNING, logger="zeroth.core.econ.budget"):
        allowed, _, _ = await enforcer.check_budget("tenant-42")
    assert allowed is True
    assert any(
        "tenant-42" in r.message and "NOT enforced" in r.message
        for r in caplog.records
    )


# -- Test 5: fail-open on timeout --


@pytest.mark.asyncio
async def test_check_budget_fail_open_timeout():
    """When httpx raises a timeout, execution is allowed (fail-open)."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=30,
        timeout=5.0,
        _transport=timeout_handler,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant-1")
    assert allowed is True
    assert spend == 0.0
    assert cap == float("inf")


# -- Test 6: BudgetExceededError attributes --


def test_budget_exceeded_error_attributes():
    """BudgetExceededError is a RuntimeError with spend and cap attributes."""
    err = BudgetExceededError("over budget", spend=105.0, cap=100.0)
    assert isinstance(err, RuntimeError)
    assert err.spend == 105.0
    assert err.cap == 100.0
    assert "over budget" in str(err)


# -- Fail-closed vs fail-open on backend error (WS-A gap #2) --


@pytest.mark.asyncio
async def test_budget_fail_closed_denies_on_backend_error(caplog):
    """fail_closed=True denies (False, 0.0, 0.0) + WARNING on backend error;
    the default (fail_closed=False) still fails open (allowed=True)."""
    import logging

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    # Fail-closed: DENY.
    closed = BudgetEnforcer(
        "http://regulus.test/v1",
        fail_closed=True,
        _transport=error_handler,
    )
    with caplog.at_level(logging.WARNING, logger="zeroth.core.econ.budget"):
        allowed, spend, cap = await closed.check_budget("tenant-closed")
    assert allowed is False
    assert spend == 0.0
    assert cap == 0.0
    assert any(
        "tenant-closed" in r.message and "CLOSED" in r.message for r in caplog.records
    )

    # Fail-open (default): ALLOW.
    open_ = BudgetEnforcer(
        "http://regulus.test/v1",
        _transport=error_handler,
    )
    allowed, spend, cap = await open_.check_budget("tenant-open")
    assert allowed is True
    assert spend == 0.0
    assert cap == float("inf")


@pytest.mark.asyncio
async def test_fail_closed_does_not_cache_denial():
    """A fail-closed denial from a transient error must NOT be cached: the next
    call re-probes and, when the backend recovers under cap, allows again."""
    calls = {"n": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(200, json={"total_cost_usd": 5, "budget_cap_usd": 100})

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        cache_ttl=60,
        fail_closed=True,
        _transport=flaky_handler,
    )
    # First call errors -> deny (must not be cached).
    allowed1, _, _ = await enforcer.check_budget("tenant-1")
    assert allowed1 is False
    # Second call re-probes (proof: HTTP was hit again) and the backend now
    # answers under cap -> allow.
    allowed2, spend2, cap2 = await enforcer.check_budget("tenant-1")
    assert allowed2 is True
    assert spend2 == 5.0
    assert cap2 == 100.0
    assert calls["n"] == 2  # denial was not served from cache


@pytest.mark.asyncio
async def test_null_cap_stays_unlimited_even_fail_closed():
    """A successfully fetched null cap is unlimited regardless of fail_closed —
    the fail-closed switch applies ONLY to the exception path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_cost_usd": 999.0, "budget_cap_usd": None})

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        fail_closed=True,
        _transport=handler,
    )
    allowed, spend, cap = await enforcer.check_budget("capless")
    assert allowed is True
    assert spend == 999.0
    assert cap == float("inf")


@pytest.mark.asyncio
async def test_external_regulus_path_unchanged():
    """With asgi_app=None (external topology) the enforcer still issues an HTTP
    GET to {base_url}/budget/status — the ASGITransport addition must not
    regress the separate-process Regulus path."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["tenant"] = request.url.params.get("tenant_id", "")
        return httpx.Response(200, json={"total_cost_usd": 1.0, "budget_cap_usd": 10.0})

    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        asgi_app=None,
        _transport=handler,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant-ext")
    assert seen["path"] == "/v1/budget/status"
    assert seen["tenant"] == "tenant-ext"
    assert allowed is True
    assert spend == 1.0
    assert cap == 10.0


# -- Integration tests: AgentRunner + BudgetEnforcer --


def _make_config() -> AgentConfig:
    return AgentConfig(
        name="budget-test",
        instruction="Return an answer.",
        model_name="test-model",
        input_model=_Input,
        output_model=_Output,
    )


def _make_provider() -> DeterministicProviderAdapter:
    return DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ok"}')])


# -- Test 7: no budget_enforcer runs normally --


@pytest.mark.asyncio
async def test_runner_no_budget_enforcer_runs_normally():
    """AgentRunner with budget_enforcer=None runs normally (no budget check)."""
    provider = _make_provider()
    runner = AgentRunner(_make_config(), provider)
    result = await runner.run({"query": "hello"})
    assert result.output_data["answer"] == "ok"


# -- Test 8: allowed budget runs normally --


@pytest.mark.asyncio
async def test_runner_allowed_budget_runs_normally():
    """AgentRunner with budget_enforcer that returns allowed=True runs normally."""
    enforcer = AsyncMock()
    enforcer.check_budget = AsyncMock(return_value=(True, 50.0, 100.0))
    provider = _make_provider()
    runner = AgentRunner(_make_config(), provider, budget_enforcer=enforcer)
    result = await runner.run(
        {"query": "hello"},
        enforcement_context={"tenant_id": "t-1"},
    )
    assert result.output_data["answer"] == "ok"
    enforcer.check_budget.assert_awaited_once_with("t-1")


# -- Test 9: over-budget raises before provider call --


@pytest.mark.asyncio
async def test_runner_over_budget_raises_before_provider_call():
    """AgentRunner with over-budget enforcer raises BudgetExceededError BEFORE provider call."""
    enforcer = AsyncMock()
    enforcer.check_budget = AsyncMock(return_value=(False, 105.0, 100.0))
    provider = AsyncMock()
    provider.ainvoke = AsyncMock()
    runner = AgentRunner(_make_config(), provider, budget_enforcer=enforcer)
    with pytest.raises(BudgetExceededError) as exc_info:
        await runner.run(
            {"query": "hello"},
            enforcement_context={"tenant_id": "t-1"},
        )
    assert exc_info.value.spend == 105.0
    assert exc_info.value.cap == 100.0
    # Provider was never called
    provider.ainvoke.assert_not_awaited()
