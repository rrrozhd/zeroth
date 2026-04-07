"""Tests for budget enforcement: BudgetEnforcer and BudgetExceededError.

Covers under-budget, over-budget, TTL cache hits, fail-open on
Regulus unavailability, and BudgetExceededError attributes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from zeroth.agent_runtime.errors import BudgetExceededError
from zeroth.econ.budget import BudgetEnforcer


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
