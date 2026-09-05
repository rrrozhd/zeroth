"""HTTP client for communicating with the sandbox sidecar service.

Used by the API container to dispatch execution requests to the sidecar
without touching the Docker socket directly.
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterable
from types import SimpleNamespace
from typing import Any

import httpx

from zeroth.integrations.execution.sandbox import SandboxBackendUnavailableError
from zeroth.integrations.http.factory import aclose_all, governed_async_client
from zeroth.integrations.sandbox.models import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)

#: Slack added on top of the execution's own deadline for the /execute call.
#: The client-wide timeout is 60s while executions default to 300s, so a
#: client-wide read timeout used to abandon any execution longer than a
#: minute; /execute now carries a per-request deadline instead.
EXECUTE_TIMEOUT_MARGIN_SECONDS = 30.0


class WorkspaceUploadConflictError(RuntimeError):
    """The workspace id was already staged; regenerate the id and retry."""


def _execute_request_timeout(timeout_seconds: float | None) -> float:
    """Per-request deadline for /execute: the execution's own bound plus slack."""
    if timeout_seconds is None or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        base = DEFAULT_EXECUTION_TIMEOUT_SECONDS
    else:
        base = timeout_seconds
    return base + EXECUTE_TIMEOUT_MARGIN_SECONDS


class SandboxSidecarClient:
    """Async HTTP client for the sandbox sidecar REST API."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        shared_secret = os.getenv("ZEROTH_SANDBOX_SIDECAR_SECRET", "")
        if not shared_secret:
            raise ValueError("ZEROTH_SANDBOX_SIDECAR_SECRET must be configured")
        # The synchronous sandbox bridge may use a different worker event loop
        # for every call. Do not retain loop-bound HTTP connections on this object.
        self._client_options = {
            "base_url": base_url,
            "timeout": timeout,
            "headers": {"X-Zeroth-Sandbox-Secret": shared_secret},
        }
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        """Submit an execution request and wait for the result.

        The HTTP deadline tracks the execution's own timeout (plus a margin)
        rather than the client-wide default, which is shorter than the default
        execution bound and used to abandon still-running executions.
        """
        resp = await self._request(
            "POST",
            "/execute",
            content=request.model_dump_json(),
            headers={"Content-Type": "application/json"},
            timeout=_execute_request_timeout(request.timeout_seconds),
        )
        resp.raise_for_status()
        return SidecarExecuteResponse.model_validate_json(resp.content)

    async def upload_workspace(
        self, workspace_id: str, tar_content: bytes | AsyncIterable[bytes]
    ) -> None:
        """Stage an uncompressed workspace tar on the sidecar (streamed PUT).

        Raises :class:`SandboxBackendUnavailableError` when the sidecar
        predates the staging channel (404/405), and
        :class:`WorkspaceUploadConflictError` when the id was already staged
        (409) -- regenerate the workspace id and retry.
        """
        resp = await self._request(
            "PUT",
            f"/workspaces/{workspace_id}",
            content=tar_content,
            headers={"Content-Type": "application/x-tar"},
        )
        if resp.status_code in (404, 405):
            raise SandboxBackendUnavailableError(
                "sidecar does not support workspace staging; upgrade the sidecar"
            )
        if resp.status_code == 409:
            raise WorkspaceUploadConflictError(
                "workspace id already staged on the sidecar; regenerate the workspace id and retry"
            )
        resp.raise_for_status()

    async def get_status(self, execution_id: str) -> SidecarStatusResponse:
        """Retrieve the status of a submitted execution."""
        resp = await self._request("GET", f"/executions/{execution_id}")
        resp.raise_for_status()
        return SidecarStatusResponse.model_validate_json(resp.content)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution."""
        resp = await self._request("POST", f"/executions/{execution_id}/cancel")
        resp.raise_for_status()

    async def health(self) -> SidecarHealthResponse:
        """Check sidecar health."""
        resp = await self._request("GET", "/health")
        resp.raise_for_status()
        return SidecarHealthResponse.model_validate_json(resp.content)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        self._closed = True
        if self._client is not None:
            await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Own default transport connections entirely within the calling event loop."""
        if getattr(self, "_closed", False):
            raise RuntimeError("sandbox sidecar client is closed")
        if self._client is not None:
            # Explicitly injected transports remain owned by their injecting caller.
            return await self._client.request(method, url, **kwargs)
        # Use an explicit short-lived cache owner: the process cache is keyed
        # by loop id, which can be recycled after the worker loop closes.
        owner = SimpleNamespace(state=SimpleNamespace())
        try:
            client = await governed_async_client(
                purpose="sandbox-sidecar",
                timeout=self._client_options["timeout"],
                base_url=self._client_options["base_url"],
                app=owner,
            )
            headers = dict(self._client_options["headers"])
            headers.update(kwargs.pop("headers", {}))
            return await client.request(method, url, headers=headers, **kwargs)
        finally:
            await aclose_all(owner)


__all__ = ["SandboxSidecarClient", "WorkspaceUploadConflictError"]
