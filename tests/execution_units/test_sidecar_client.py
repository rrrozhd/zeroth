"""Tests for the SandboxSidecarClient HTTP client."""

from __future__ import annotations

import pytest
import httpx

from zeroth.execution_units.sidecar_client import SandboxSidecarClient
from zeroth.sandbox_sidecar.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)


def _make_transport(handler):
    """Build an httpx.MockTransport from an async handler."""
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_execute_sends_post() -> None:
    """Client POSTs to /execute and parses the response."""
    response_body = SidecarExecuteResponse(
        execution_id="exec-1",
        status="completed",
        returncode=0,
        stdout="ok\n",
        stderr="",
        duration_seconds=0.5,
        timed_out=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).endswith("/execute")
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, json=response_body.model_dump())

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    request = SidecarExecuteRequest(
        execution_id="exec-1",
        image="python:3.12",
        command=["python", "-c", "print('ok')"],
    )
    result = await client.execute(request)

    assert result.execution_id == "exec-1"
    assert result.status == "completed"
    assert result.returncode == 0
    assert result.stdout == "ok\n"

    await client.close()


@pytest.mark.asyncio
async def test_get_status() -> None:
    """Client GETs /executions/{id} and parses the response."""
    response_body = SidecarStatusResponse(
        execution_id="exec-2",
        status="completed",
        returncode=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/executions/exec-2" in str(request.url)
        return httpx.Response(200, json=response_body.model_dump())

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    result = await client.get_status("exec-2")

    assert result.execution_id == "exec-2"
    assert result.status == "completed"

    await client.close()


@pytest.mark.asyncio
async def test_cancel() -> None:
    """Client POSTs to /executions/{id}/cancel."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/executions/exec-3/cancel" in str(request.url)
        return httpx.Response(200, json={"status": "cancelled"})

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    await client.cancel("exec-3")  # Should not raise

    await client.close()


@pytest.mark.asyncio
async def test_health() -> None:
    """Client GETs /health and parses the response."""
    response_body = SidecarHealthResponse(status="ok", docker_available=True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/health")
        return httpx.Response(200, json=response_body.model_dump())

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    result = await client.health()

    assert result.status == "ok"
    assert result.docker_available is True

    await client.close()


@pytest.mark.asyncio
async def test_execute_http_error() -> None:
    """Client raises on HTTP 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal error"})

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    request = SidecarExecuteRequest(
        execution_id="err-1",
        image="python:3.12",
        command=["false"],
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.execute(request)

    await client.close()


@pytest.mark.asyncio
async def test_get_status_not_found() -> None:
    """Client raises on HTTP 404 for unknown execution."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    transport = _make_transport(handler)
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(transport=transport, base_url="http://sidecar:8001")

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_status("nonexistent")

    await client.close()
