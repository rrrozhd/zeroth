"""HTTP routes for Studio workflow edit leases."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from zeroth.studio.app import require_studio_principal
from zeroth.studio.leases.models import WorkflowLeaseConflict


class WorkflowLeaseResponse(BaseModel):
    """Lease payload returned by Studio lease routes."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workspace_id: str
    lease_token: str
    expires_at: str


class WorkflowLeasePingRequest(BaseModel):
    """Renew request payload for an existing workflow lease."""

    model_config = ConfigDict(extra="forbid")

    lease_token: str


def register_session_routes(app: FastAPI) -> None:
    """Register Studio lease acquire/renew/release routes."""

    router = APIRouter(tags=["studio-sessions"])

    @router.post(
        "/studio/workflows/{workflow_id}/leases",
        response_model=WorkflowLeaseResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def acquire_lease(workflow_id: str, request: Request) -> WorkflowLeaseResponse:
        principal = require_studio_principal(request)
        lease_service = request.app.state.bootstrap.lease_service
        lease = lease_service.acquire_lease(
            workflow_id=workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            subject=principal.subject,
        )
        if lease is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        if isinstance(lease, WorkflowLeaseConflict):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "workflow_id": lease.workflow_id,
                    "workspace_id": lease.workspace_id,
                    "lease_token": lease.lease_token,
                    "expires_at": lease.expires_at.isoformat(),
                },
            )
        return WorkflowLeaseResponse(
            workflow_id=lease.workflow_id,
            workspace_id=lease.workspace_id,
            lease_token=lease.lease_token,
            expires_at=lease.expires_at.isoformat(),
        )

    @router.post(
        "/studio/workflows/{workflow_id}/leases/ping",
        response_model=WorkflowLeaseResponse,
    )
    async def renew_lease(
        workflow_id: str,
        payload: WorkflowLeasePingRequest,
        request: Request,
    ) -> WorkflowLeaseResponse:
        principal = require_studio_principal(request)
        lease_service = request.app.state.bootstrap.lease_service
        lease = lease_service.renew_lease(
            workflow_id=workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            lease_token=payload.lease_token,
        )
        if lease is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        return WorkflowLeaseResponse(
            workflow_id=lease.workflow_id,
            workspace_id=lease.workspace_id,
            lease_token=lease.lease_token,
            expires_at=lease.expires_at.isoformat(),
        )

    @router.delete(
        "/studio/workflows/{workflow_id}/leases/{lease_token}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def release_lease(
        workflow_id: str,
        lease_token: str,
        request: Request,
    ) -> Response:
        principal = require_studio_principal(request)
        lease_service = request.app.state.bootstrap.lease_service
        released = lease_service.release_lease(
            workflow_id=workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            lease_token=lease_token,
        )
        if not released:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)
