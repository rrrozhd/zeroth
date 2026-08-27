"""FastAPI sidecar application for sandboxed Docker execution.

This service runs as a separate process with Docker socket access.
The main API container communicates with it over HTTP, never touching
the Docker socket directly.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarHealthResponse,
    SidecarStatusResponse,
)
from zeroth.integrations.sandbox.staging import (
    DEFAULT_MAX_TAR_MEMBERS,
    WorkspaceStore,
    WorkspaceValidationCode,
    WorkspaceValidationError,
    resolve_max_workspace_bytes,
    resolve_workspace_spool_dir,
    resolve_workspace_ttl_seconds,
)
from zeroth.integrations.sandbox.staging_models import SidecarWorkspaceUploadResponse

logger = logging.getLogger(__name__)

#: How often the lifespan task sweeps expired, unclaimed workspace spools.
WORKSPACE_SWEEP_INTERVAL_SECONDS = 60.0


async def _sweep_workspaces_forever() -> None:
    """Periodically expire unclaimed workspace spools past their TTL."""
    while True:
        await asyncio.sleep(WORKSPACE_SWEEP_INTERVAL_SECONDS)
        try:
            await workspace_store.sweep(resolve_workspace_ttl_seconds())
        except Exception:  # noqa: BLE001 - the sweep must never kill its loop
            logger.warning("Workspace TTL sweep failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reclaim stale spools from a prior process, then sweep on a timer."""
    workspace_store.startup_gc()
    sweep_task = asyncio.create_task(_sweep_workspaces_forever())
    try:
        yield
    finally:
        sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            await sweep_task


app = FastAPI(title="Zeroth Sandbox Sidecar", lifespan=_lifespan)
executor = SidecarExecutor()
workspace_store = WorkspaceStore(resolve_workspace_spool_dir())
executor.workspace_store = workspace_store
SIDECAR_SECRET_ENV = "ZEROTH_SANDBOX_SIDECAR_SECRET"
SIDECAR_SECRET_HEADER = "X-Zeroth-Sandbox-Secret"

#: Codes whose HTTP status is not the generic 422: a duplicate id is a
#: conflict, and the mid-stream byte cap is a payload-size refusal.
_WORKSPACE_ERROR_STATUS: dict[WorkspaceValidationCode, int] = {
    WorkspaceValidationCode.WORKSPACE_DUPLICATE: status.HTTP_409_CONFLICT,
    WorkspaceValidationCode.TAR_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
}


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


@app.put(
    "/workspaces/{workspace_id}",
    response_model=SidecarWorkspaceUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_sidecar_secret)],
)
async def upload_workspace(
    workspace_id: str, request: Request
) -> SidecarWorkspaceUploadResponse:
    """Stage an uncompressed workspace tar for a later execution.

    The body streams straight into the store, which enforces the byte cap
    mid-stream and validates the spooled archive before the id registers.
    Every refusal carries the store's fixed message template — member names
    never reach a response.
    """
    try:
        summary = await workspace_store.ingest(
            workspace_id,
            request.stream(),
            max_raw_bytes=resolve_max_workspace_bytes(),
            max_members=DEFAULT_MAX_TAR_MEMBERS,
        )
    except WorkspaceValidationError as exc:
        raise HTTPException(
            status_code=_WORKSPACE_ERROR_STATUS.get(
                exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT
            ),
            detail=str(exc),
        ) from exc
    return SidecarWorkspaceUploadResponse(
        workspace_id=workspace_id,
        raw_bytes=summary.raw_bytes,
        member_count=summary.member_count,
        total_file_bytes=summary.total_file_bytes,
    )


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
