"""HTTP client for communicating with the sandbox sidecar service.

Used by the API container to dispatch execution requests to the sidecar
without touching the Docker socket directly.
"""

from __future__ import annotations

import httpx

from zeroth.sandbox_sidecar.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)


class SandboxSidecarClient:
    """Async HTTP client for the sandbox sidecar REST API."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        """Submit an execution request and wait for the result."""
        resp = await self._client.post(
            "/execute",
            content=request.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return SidecarExecuteResponse.model_validate_json(resp.content)

    async def get_status(self, execution_id: str) -> SidecarStatusResponse:
        """Retrieve the status of a submitted execution."""
        resp = await self._client.get(f"/executions/{execution_id}")
        resp.raise_for_status()
        return SidecarStatusResponse.model_validate_json(resp.content)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution."""
        resp = await self._client.post(f"/executions/{execution_id}/cancel")
        resp.raise_for_status()

    async def health(self) -> SidecarHealthResponse:
        """Check sidecar health."""
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return SidecarHealthResponse.model_validate_json(resp.content)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


__all__ = ["SandboxSidecarClient"]
