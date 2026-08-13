"""FastAPI sidecar application for sandboxed Docker execution.

This service runs as a separate process with Docker socket access.
The main API container communicates with it over HTTP, never touching
the Docker socket directly.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)

app = FastAPI(title="Zeroth Sandbox Sidecar")
executor = SidecarExecutor()
SIDECAR_SECRET_ENV = "ZEROTH_SANDBOX_SIDECAR_SECRET"
SIDECAR_SECRET_HEADER = "X-Zeroth-Sandbox-Secret"


def require_sidecar_secret(
    presented_secret: Annotated[
        str | None,
        Header(alias=SIDECAR_SECRET_HEADER),
    ] = None,
) -> None:
    """Fail closed unless the caller presents the configured shared secret."""
    expected_secret = os.getenv(SIDECAR_SECRET_ENV, "")
    presented = presented_secret or ""
    matches = hmac.compare_digest(expected_secret.encode(), presented.encode())
    if not expected_secret or not presented_secret or not matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid sandbox sidecar credentials",
        )


@app.post(
    "/execute",
    response_model=SidecarExecuteResponse,
    dependencies=[Depends(require_sidecar_secret)],
)
async def execute(request: SidecarExecuteRequest) -> SidecarExecuteResponse:
    """Execute a command in an isolated Docker container."""
    try:
        return await executor.execute(request)
    except ValueError as exc:
        # A rejected timeout is a bad request, not a server fault. Without this
        # the resolver's ValueError reached no handler and surfaced as a 500,
        # where the previous unbounded code answered 200 with timed_out=True.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@app.get(
    "/executions/{execution_id}",
    response_model=SidecarStatusResponse,
    dependencies=[Depends(require_sidecar_secret)],
)
async def get_status(execution_id: str) -> SidecarStatusResponse:
    """Get the status of a previously submitted execution."""
    result = await executor.get_status(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@app.post(
    "/executions/{execution_id}/cancel",
    dependencies=[Depends(require_sidecar_secret)],
)
async def cancel(execution_id: str) -> dict[str, str]:
    """Cancel a running execution."""
    found = await executor.cancel(execution_id)
    if not found:
        raise HTTPException(status_code=404, detail="Execution not found")
    return {"status": "cancelled"}


@app.get("/health", response_model=SidecarHealthResponse)
async def health() -> SidecarHealthResponse:
    """Check Docker daemon availability."""
    available = await executor.check_health()
    return SidecarHealthResponse(docker_available=available)


__all__ = ["app"]
