"""FastAPI sidecar application for sandboxed Docker execution.

This service runs as a separate process with Docker socket access.
The main API container communicates with it over HTTP, never touching
the Docker socket directly.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from zeroth.core.sandbox_sidecar.executor import SidecarExecutor
from zeroth.core.sandbox_sidecar.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)

app = FastAPI(title="Zeroth Sandbox Sidecar")
executor = SidecarExecutor()


@app.post("/execute", response_model=SidecarExecuteResponse)
async def execute(request: SidecarExecuteRequest) -> SidecarExecuteResponse:
    """Execute a command in an isolated Docker container."""
    return await executor.execute(request)


@app.get("/executions/{execution_id}", response_model=SidecarStatusResponse)
async def get_status(execution_id: str) -> SidecarStatusResponse:
    """Get the status of a previously submitted execution."""
    result = await executor.get_status(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@app.post("/executions/{execution_id}/cancel")
async def cancel(execution_id: str) -> dict[str, str]:
    """Cancel a running execution."""
    await executor.cancel(execution_id)
    return {"status": "cancelled"}


@app.get("/health", response_model=SidecarHealthResponse)
async def health() -> SidecarHealthResponse:
    """Check Docker daemon availability."""
    available = await executor.check_health()
    return SidecarHealthResponse(docker_available=available)


__all__ = ["app"]
