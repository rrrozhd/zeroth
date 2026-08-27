"""ZER-37 client-side staging: upload_workspace and the /execute deadline fix."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from zeroth.integrations.execution.sandbox import SandboxBackendUnavailableError
from zeroth.integrations.execution.sidecar_client import (
    SandboxSidecarClient,
    WorkspaceUploadConflictError,
)
from zeroth.integrations.sandbox.models import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    SidecarExecuteRequest,
    SidecarExecuteResponse,
)

UPLOAD_RESPONSE = {
    "workspace_id": "ws-1",
    "raw_bytes": 6,
    "member_count": 1,
    "total_file_bytes": 2,
}


def _client_over(handler) -> SandboxSidecarClient:
    client = SandboxSidecarClient.__new__(SandboxSidecarClient)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://sidecar:8001"
    )
    return client


@pytest.mark.asyncio
async def test_upload_workspace_streams_the_tar_body() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = await request.aread()
        return httpx.Response(201, json=UPLOAD_RESPONSE)

    async def chunks() -> AsyncIterator[bytes]:
        for chunk in (b"tar-", b"stream-", b"chunks"):
            yield chunk

    client = _client_over(handler)
    result = await client.upload_workspace("ws-1", chunks())
    await client.close()

    assert result is None
    assert seen["method"] == "PUT"
    assert seen["path"] == "/workspaces/ws-1"
    assert seen["content_type"] == "application/x-tar"
    assert seen["body"] == b"tar-stream-chunks"


@pytest.mark.asyncio
async def test_upload_workspace_accepts_plain_bytes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert await request.aread() == b"raw-tar-bytes"
        return httpx.Response(201, json=UPLOAD_RESPONSE)

    client = _client_over(handler)
    await client.upload_workspace("ws-1", b"raw-tar-bytes")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 405])
async def test_upload_to_an_old_sidecar_raises_an_actionable_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    client = _client_over(handler)

    with pytest.raises(SandboxBackendUnavailableError, match="upgrade the sidecar"):
        await client.upload_workspace("ws-1", b"tar")

    await client.close()


@pytest.mark.asyncio
async def test_upload_conflict_asks_for_a_regenerated_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "workspace id has already been staged"})

    client = _client_over(handler)

    with pytest.raises(WorkspaceUploadConflictError, match="regenerate"):
        await client.upload_workspace("ws-1", b"tar")

    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 413, 422, 500])
async def test_other_upload_failures_stay_typed_http_errors(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    client = _client_over(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_workspace("ws-1", b"tar")

    await client.close()


def _execute_response(execution_id: str) -> dict[str, object]:
    return SidecarExecuteResponse(
        execution_id=execution_id, status="completed", returncode=0
    ).model_dump()


@pytest.mark.asyncio
async def test_execute_deadline_tracks_the_requested_execution_timeout() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=_execute_response("exec-t"))

    client = _client_over(handler)
    await client.execute(
        SidecarExecuteRequest(
            execution_id="exec-t", image="python:3.12", command=["true"], timeout_seconds=250.0
        )
    )
    await client.close()

    assert seen["timeout"]["read"] == 280.0  # 250s execution + 30s margin


@pytest.mark.asyncio
async def test_execute_deadline_covers_the_default_execution_bound() -> None:
    """The latent defect: the client-wide 60s timeout abandoned 300s runs."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=_execute_response("exec-d"))

    client = _client_over(handler)
    await client.execute(
        SidecarExecuteRequest(execution_id="exec-d", image="python:3.12", command=["true"])
    )
    await client.close()

    assert seen["timeout"]["read"] >= DEFAULT_EXECUTION_TIMEOUT_SECONDS
    assert seen["timeout"]["read"] == DEFAULT_EXECUTION_TIMEOUT_SECONDS + 30.0


@pytest.mark.asyncio
async def test_execute_deadline_falls_back_for_unusable_timeouts() -> None:
    """A bogus timeout gets the sidecar's 422; the HTTP deadline stays finite."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(422, json={"detail": "timeout_seconds must be positive"})

    client = _client_over(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.execute(
            SidecarExecuteRequest(
                execution_id="exec-b",
                image="python:3.12",
                command=["true"],
                timeout_seconds=float("inf"),
            )
        )
    await client.close()

    assert seen["timeout"]["read"] == DEFAULT_EXECUTION_TIMEOUT_SECONDS + 30.0
