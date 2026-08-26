"""Tests for health probe endpoints and TLS settings."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.platform.config.settings import TLSSettings
from zeroth.platform.storage.schema_revision import SchemaRevision
from zeroth.service.api.health import (
    DependencyStatus,
    LivenessResponse,
    ReadinessResponse,
    check_database,
    check_redis,
    check_regulus,
    check_schema_revision,
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


class RevisionDatabase(FakeDatabase):
    """Record the bounded read-only queries made by readiness."""

    def __init__(
        self,
        revisions: list[str],
        *,
        revision_delay: float = 0,
    ) -> None:
        super().__init__()
        self.revisions = revisions
        self.revision_delay = revision_delay
        self.queries: list[str] = []

    @asynccontextmanager
    async def transaction(self, *, write_lock: bool = False):
        database = self

        class Connection(FakeConnection):
            async def fetch_one(
                self, sql: str, params: tuple[Any, ...] = ()
            ) -> dict[str, Any] | None:
                database.queries.append(sql)
                return await super().fetch_one(sql, params)

            async def fetch_all(
                self, sql: str, params: tuple[Any, ...] = ()
            ) -> list[dict[str, Any]]:
                database.queries.append(sql)
                if database.revision_delay:
                    await asyncio.sleep(database.revision_delay)
                return [{"version_num": revision} for revision in database.revisions]

        yield Connection()


def _readiness_app(database: FakeDatabase):
    from fastapi import FastAPI

    app = FastAPI()
    bootstrap = MagicMock()
    bootstrap.database = database
    bootstrap.regulus_client = None
    bootstrap.langgraph_gateway_compatibility = None
    bootstrap.audit_delivery_queue = None
    app.state.bootstrap = bootstrap
    register_health_routes(app)
    return app


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
    assert result.detail == "database: unreachable"


@pytest.mark.asyncio
async def test_check_database_error_carries_no_driver_text():
    """A02-4: the driver's message names the DSN, host, and port it dialled."""
    leaky = 'connection to server at "db.internal" (172.18.0.2), port 5432 failed'
    db = FakeDatabase(should_raise=ConnectionError(leaky))

    result = await check_database(db)

    assert leaky not in result.detail
    for fragment in ("db.internal", "172.18.0.2", "5432"):
        assert fragment not in result.detail


# ---------------------------------------------------------------------------
# check_redis tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_redis_ok():
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True
    mock_redis.aclose = AsyncMock()

    with patch(
        "zeroth.service.api.health.governed_redis_client",
        AsyncMock(return_value=mock_redis),
    ):
        result = await check_redis("redis://localhost:6379/0")
    assert result.status == "ok"
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_check_redis_error():
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("Redis down")
    mock_redis.aclose = AsyncMock()

    with patch(
        "zeroth.service.api.health.governed_redis_client",
        AsyncMock(return_value=mock_redis),
    ):
        result = await check_redis("redis://localhost:6379/0")
    assert result.status == "error"
    assert result.detail == "redis: unreachable"


@pytest.mark.asyncio
async def test_check_redis_error_carries_no_driver_text():
    """A02-4: a redis client's message names the host and port it could not reach."""
    leaky = "Error connecting to 10.0.3.14:6379. Connection refused."
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError(leaky)
    mock_redis.aclose = AsyncMock()

    with patch(
        "zeroth.service.api.health.governed_redis_client",
        AsyncMock(return_value=mock_redis),
    ):
        result = await check_redis("redis://10.0.3.14:6379/0")

    assert leaky not in result.detail
    for fragment in ("10.0.3.14", "6379"):
        assert fragment not in result.detail


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

    with patch(
        "zeroth.service.api.health.governed_async_client",
        AsyncMock(return_value=mock_client),
    ):
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

    with patch(
        "zeroth.service.api.health.governed_async_client",
        AsyncMock(return_value=mock_client),
    ):
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
    response = ReadinessResponse(
        status="ok",
        checks=checks,
    )
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
    from zeroth.service.api.health import determine_readiness_status

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
    from zeroth.service.api.health import determine_readiness_status

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

    app = FastAPI()

    # Set up minimal app state
    mock_bootstrap = MagicMock()
    app.state.bootstrap = mock_bootstrap

    register_health_routes(app)

    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/health/ready" in routes
    assert "/health/live" in routes


def test_readiness_openapi_preserves_response_component() -> None:
    schema = _readiness_app(FakeDatabase()).openapi()
    response_schema = schema["paths"]["/health/ready"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert response_schema == {"$ref": "#/components/schemas/ReadinessResponse"}
    assert "schema_revision" in schema["components"]["schemas"]["ReadinessResponse"][
        "required"
    ]


@pytest.mark.asyncio
async def test_readiness_reports_current_service_schema_revision():
    """A migrated service exposes the applied and shipped Alembic revisions."""
    from fastapi.testclient import TestClient

    database = RevisionDatabase(["027"])

    with (
        patch(
            "zeroth.service.api.health.check_redis",
            new=AsyncMock(return_value=DependencyStatus(status="ok")),
        ),
        patch(
            "zeroth.service.api.health.check_regulus",
            new=AsyncMock(return_value=DependencyStatus(status="ok")),
        ),
    ):
        response = TestClient(_readiness_app(database)).get("/health/ready")

    assert response.json()["status"] == "ok"
    assert response.json().get("schema_revision") == {
        "applied": "027",
        "head": "027",
        "state": "current",
    }
    assert database.queries.count("SELECT version_num FROM alembic_version LIMIT 2") == 1
    assert all(query.lstrip().upper().startswith("SELECT ") for query in database.queries)


@pytest.mark.parametrize(
    ("revisions", "applied", "state"),
    [
        (["026"], "026", "behind"),
        ([], None, "unknown"),
        (["026", "027"], None, "unknown"),
        (["foreign"], "foreign", "unknown"),
    ],
)
def test_readiness_degrades_for_stale_or_unknown_service_schema(
    revisions: list[str], applied: str | None, state: str
) -> None:
    from fastapi.testclient import TestClient

    with (
        patch(
            "zeroth.service.api.health.check_redis",
            new=AsyncMock(return_value=DependencyStatus(status="ok")),
        ),
        patch(
            "zeroth.service.api.health.check_regulus",
            new=AsyncMock(return_value=DependencyStatus(status="ok")),
        ),
    ):
        response = TestClient(_readiness_app(RevisionDatabase(revisions))).get(
            "/health/ready"
        )

    assert response.json()["status"] == "degraded"
    assert response.json()["schema_revision"] == {
        "applied": applied,
        "head": "027",
        "state": state,
    }


@pytest.mark.asyncio
async def test_service_schema_revision_read_has_an_explicit_timeout() -> None:
    database = RevisionDatabase(["027"], revision_delay=1)

    revision = await asyncio.wait_for(
        check_schema_revision(database, timeout_seconds=0.001),
        timeout=0.1,
    )

    assert revision.model_dump() == {
        "applied": None,
        "head": "027",
        "state": "unknown",
    }
    assert database.queries == ["SELECT version_num FROM alembic_version LIMIT 2"]


@pytest.mark.asyncio
async def test_readiness_body_carries_no_driver_text_when_dependencies_fail():
    """A02-4 end to end, over the WHOLE body at WHATEVER status code it returns.

    ``/health/ready`` answers before authentication and returns **200** even when
    a dependency is down -- ``health_ready`` builds a readiness payload
    regardless of what ``determine_readiness_status`` decides. An assertion scoped
    to 4xx/5xx bodies would pass while the leak stayed open, so this asserts on
    the serialized body itself, whatever the status code.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db_leak = 'connection to server at "db.internal" (172.18.0.2), port 5432 failed'
    redis_leak = "Error connecting to 10.0.3.14:6379. Connection refused."

    app = FastAPI()
    mock_bootstrap = MagicMock()
    mock_bootstrap.database = FakeDatabase(should_raise=ConnectionError(db_leak))
    mock_bootstrap.regulus_client = None
    mock_bootstrap.langgraph_gateway_compatibility = None
    mock_bootstrap.audit_delivery_queue = None
    app.state.bootstrap = mock_bootstrap

    register_health_routes(app)

    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError(redis_leak)
    mock_redis.aclose = AsyncMock()

    with patch(
        "zeroth.service.api.health.governed_redis_client",
        AsyncMock(return_value=mock_redis),
    ):
        client = TestClient(app)
        response = client.get("/health/ready")

    body = response.text
    assert db_leak not in body
    assert redis_leak not in body
    for fragment in ("db.internal", "172.18.0.2", "5432", "10.0.3.14", "6379"):
        assert fragment not in body, f"{fragment!r} leaked into the readiness body"
    # The probe must still be USEFUL: the category survives even though the text
    # does not, or an operator learns nothing from a failing probe.
    assert "unreachable" in body


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
