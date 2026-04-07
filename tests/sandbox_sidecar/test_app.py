"""Tests for the sandbox sidecar FastAPI application."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from zeroth.sandbox_sidecar.app import app
from zeroth.sandbox_sidecar.models import (
    SidecarExecuteResponse,
    SidecarStatusResponse,
)


@pytest.fixture
def mock_executor() -> AsyncMock:
    """Create a mock SidecarExecutor."""
    return AsyncMock()


@pytest.fixture
async def client(mock_executor: AsyncMock):
    """Create a test client with mocked executor."""
    with patch("zeroth.sandbox_sidecar.app.executor", mock_executor):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_execute_endpoint(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """POST /execute returns execution response."""
    mock_executor.execute.return_value = SidecarExecuteResponse(
        execution_id="test-123",
        status="completed",
        returncode=0,
        stdout="hello\n",
        stderr="",
        duration_seconds=1.5,
        timed_out=False,
    )

    resp = await client.post(
        "/execute",
        json={
            "execution_id": "test-123",
            "image": "python:3.12-slim",
            "command": ["python", "-c", "print('hello')"],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_id"] == "test-123"
    assert data["status"] == "completed"
    assert data["returncode"] == 0
    assert data["stdout"] == "hello\n"
    mock_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_status_found(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """GET /executions/{id} returns status when execution exists."""
    mock_executor.get_status.return_value = SidecarStatusResponse(
        execution_id="test-456",
        status="completed",
        returncode=0,
    )

    resp = await client.get("/executions/test-456")

    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_id"] == "test-456"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_get_status_not_found(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """GET /executions/{id} returns 404 when execution not found."""
    mock_executor.get_status.return_value = None

    resp = await client.get("/executions/unknown-id")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cancel_endpoint(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """POST /executions/{id}/cancel returns cancelled status."""
    resp = await client.post("/executions/test-789/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    mock_executor.cancel.assert_awaited_once_with("test-789")


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """GET /health returns docker availability."""
    mock_executor.check_health.return_value = True

    resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["docker_available"] is True


@pytest.mark.asyncio
async def test_health_docker_unavailable(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """GET /health reports docker unavailable."""
    mock_executor.check_health.return_value = False

    resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["docker_available"] is False


@pytest.mark.asyncio
async def test_execute_with_all_fields(client: AsyncClient, mock_executor: AsyncMock) -> None:
    """POST /execute with all optional fields populated."""
    mock_executor.execute.return_value = SidecarExecuteResponse(
        execution_id="full-test",
        status="completed",
        returncode=0,
        duration_seconds=2.0,
    )

    resp = await client.post(
        "/execute",
        json={
            "execution_id": "full-test",
            "image": "python:3.12-slim",
            "command": ["python", "-c", "pass"],
            "input_text": "some input",
            "timeout_seconds": 30.0,
            "environment": {"FOO": "bar"},
            "working_directory": "/app",
            "cpu_cores": 1.0,
            "memory_mb": 256,
            "max_processes": 50,
            "network_access": False,
        },
    )

    assert resp.status_code == 200
    # Verify the request was parsed correctly
    call_args = mock_executor.execute.call_args[0][0]
    assert call_args.execution_id == "full-test"
    assert call_args.network_access is False
    assert call_args.cpu_cores == 1.0
    assert call_args.memory_mb == 256
