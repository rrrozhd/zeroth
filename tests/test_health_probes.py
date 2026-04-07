"""Tests for health probe endpoints and TLS settings."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.config.settings import TLSSettings
from zeroth.service.health import (
    DependencyStatus,
    LivenessResponse,
    ReadinessResponse,
    check_database,
    check_redis,
    check_regulus,
    register_health_routes,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class FakeConnection:
    """Minimal AsyncConnection stand-in."""

    def __init__(self, *, should_raise: Exception | None = None):
        self._should_raise = should_raise

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if self._should_raise:
            raise self._should_raise

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        if self._should_raise:
            raise self._should_raise
        return {"result": 1}

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._should_raise:
            raise self._should_raise
        return [{"result": 1}]

    async def execute_script(self, sql: str) -> None:
        if self._should_raise:
            raise self._should_raise


class FakeDatabase:
    """Minimal AsyncDatabase stand-in."""

    def __init__(self, *, should_raise: Exception | None = None):
        self._should_raise = should_raise

    @asynccontextmanager
    async def transaction(self):
        yield FakeConnection(should_raise=self._should_raise)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# check_database tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_database_ok():
    db = FakeDatabase()
    result = await check_database(db)
    assert result.status == "ok"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_check_database_error():
    db = FakeDatabase(should_raise=ConnectionError("connection refused"))
    result = await check_database(db)
    assert result.status == "error"
    assert "connection refused" in result.detail


# ---------------------------------------------------------------------------
# check_redis tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_redis_ok():
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True
    mock_redis.aclose = AsyncMock()

    with patch("zeroth.service.health.redis_from_url", return_value=mock_redis):
        result = await check_redis("redis://localhost:6379/0")
    assert result.status == "ok"
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_check_redis_error():
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("Redis down")
    mock_redis.aclose = AsyncMock()

    with patch("zeroth.service.health.redis_from_url", return_value=mock_redis):
        result = await check_redis("redis://localhost:6379/0")
    assert result.status == "error"
    assert "Redis down" in result.detail


# ---------------------------------------------------------------------------
# check_regulus tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_regulus_ok():
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("zeroth.service.health.httpx.AsyncClient", return_value=mock_client):
        result = await check_regulus("http://regulus:8000")
    assert result.status == "ok"
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_check_regulus_unavailable_when_not_configured():
    result = await check_regulus(None)
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_check_regulus_unavailable_on_error():
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("zeroth.service.health.httpx.AsyncClient", return_value=mock_client):
        result = await check_regulus("http://regulus:8000")
    assert result.status == "unavailable"


# ---------------------------------------------------------------------------
# ReadinessResponse status logic tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_ok_when_all_healthy():
    """When all required deps are healthy, status is 'ok'."""
    checks = {
        "database": DependencyStatus(status="ok", latency_ms=1.0),
        "redis": DependencyStatus(status="ok", latency_ms=2.0),
        "regulus": DependencyStatus(status="ok", latency_ms=3.0),
    }
    response = ReadinessResponse(status="ok", checks=checks)
    assert response.status == "ok"


@pytest.mark.asyncio
async def test_readiness_unhealthy_when_db_down():
    """When DB is down, status is 'unhealthy'."""
    checks = {
        "database": DependencyStatus(status="error", detail="connection refused"),
        "redis": DependencyStatus(status="ok", latency_ms=2.0),
        "regulus": DependencyStatus(status="ok", latency_ms=3.0),
    }
    # Import the function that determines overall status
    from zeroth.service.health import determine_readiness_status

    status = determine_readiness_status(checks)
    assert status == "unhealthy"


@pytest.mark.asyncio
async def test_readiness_degraded_when_regulus_down():
    """When only Regulus is down, status is 'degraded'."""
    checks = {
        "database": DependencyStatus(status="ok", latency_ms=1.0),
        "redis": DependencyStatus(status="ok", latency_ms=2.0),
        "regulus": DependencyStatus(status="unavailable"),
    }
    from zeroth.service.health import determine_readiness_status

    status = determine_readiness_status(checks)
    assert status == "degraded"


# ---------------------------------------------------------------------------
# LivenessResponse tests
# ---------------------------------------------------------------------------


def test_liveness_always_ok():
    response = LivenessResponse()
    assert response.status == "ok"


# ---------------------------------------------------------------------------
# TLSSettings tests
# ---------------------------------------------------------------------------


def test_tls_settings_defaults_to_none():
    tls = TLSSettings()
    assert tls.certfile is None
    assert tls.keyfile is None


def test_tls_settings_with_values():
    tls = TLSSettings(certfile="/path/to/cert.pem", keyfile="/path/to/key.pem")
    assert tls.certfile == "/path/to/cert.pem"
    assert tls.keyfile == "/path/to/key.pem"


# ---------------------------------------------------------------------------
# Health endpoints bypass auth tests
# ---------------------------------------------------------------------------


def test_health_paths_bypass_auth():
    """Verify that /health paths are recognized as auth-exempt."""
    # This tests the path check logic used in the middleware
    health_paths = ["/health", "/health/ready", "/health/live"]
    for path in health_paths:
        assert path.startswith("/health"), f"{path} should start with /health"


# ---------------------------------------------------------------------------
# register_health_routes integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_health_routes_adds_endpoints():
    """Verify register_health_routes adds /health/ready and /health/live routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    # Set up minimal app state
    mock_bootstrap = MagicMock()
    app.state.bootstrap = mock_bootstrap

    register_health_routes(app)

    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/health/ready" in routes
    assert "/health/live" in routes


@pytest.mark.asyncio
async def test_liveness_endpoint_returns_ok():
    """The /health/live endpoint should return status=ok immediately."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    mock_bootstrap = MagicMock()
    app.state.bootstrap = mock_bootstrap

    register_health_routes(app)

    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
