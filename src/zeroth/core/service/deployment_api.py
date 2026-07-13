"""Deployment REST API: listing, creation, and rollback.

Surfaces deployments created through the medium-code path (code-defined
graphs deployed via :class:`DeploymentService`) so operator UIs — the
bundled console in particular — can display every persisted deployment
version alongside the one this service instance is currently serving,
and lets a published graph be deployed without writing Python. Serving a
newly created deployment still requires a restart with
``ZEROTH_DEPLOYMENT_REF`` pointed at it (one deployment per process).
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from zeroth.core.deployments.service import DeploymentError
from zeroth.core.service.authorization import Permission, require_permission


class DeploymentSummaryResponse(BaseModel):
    """One persisted deployment version, as shown in the console."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    version: int
    graph_version_ref: str
    status: str
    serving: bool
    created_at: str


class CreateDeploymentRequest(BaseModel):
    """Request body for POST /v1/deployments."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    graph_id: str
    graph_version: int | None = None


class RollbackDeploymentRequest(BaseModel):
    """Request body for POST /v1/deployments/{deployment_ref}/rollback."""

    model_config = ConfigDict(extra="forbid")

    target_graph_version: int


def _summary(deployment, serving) -> DeploymentSummaryResponse:
    return DeploymentSummaryResponse(
        deployment_ref=deployment.deployment_ref,
        version=deployment.version,
        graph_version_ref=deployment.graph_version_ref,
        status=deployment.status.value,
        serving=(
            serving is not None
            and deployment.deployment_ref == serving.deployment_ref
            and deployment.version == serving.version
        ),
        created_at=deployment.created_at.isoformat(),
    )


def register_deployment_routes(app: FastAPI | APIRouter) -> None:
    """Register deployment routes on the FastAPI app."""

    @app.get("/deployments", response_model=list[DeploymentSummaryResponse])
    async def list_deployments(request: Request) -> list[DeploymentSummaryResponse]:
        """All persisted deployment versions, newest first.

        ``serving`` marks the deployment version this service instance was
        bootstrapped with (the one /health reports).
        """
        principal = await require_permission(request, Permission.DEPLOYMENT_READ)
        bootstrap = request.app.state.bootstrap
        serving = bootstrap.deployment
        deployments = await bootstrap.deployment_service.list(
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
        deployments.sort(key=lambda d: d.created_at, reverse=True)
        return [_summary(d, serving) for d in deployments]

    @app.post(
        "/deployments",
        response_model=DeploymentSummaryResponse,
        status_code=201,
    )
    async def create_deployment(
        request: Request, body: CreateDeploymentRequest
    ) -> DeploymentSummaryResponse:
        """Create a deployment version from a published graph.

        Completes the authoring loop without Python: publish the graph
        (studio API), deploy it here, then restart the service with
        ``ZEROTH_DEPLOYMENT_REF`` set to the new ref to serve it.
        """
        principal = await require_permission(request, Permission.DEPLOYMENT_ADMIN)
        bootstrap = request.app.state.bootstrap
        try:
            deployment = await bootstrap.deployment_service.deploy(
                body.deployment_ref,
                body.graph_id,
                body.graph_version,
                tenant_id=principal.tenant_id,
                workspace_id=principal.workspace_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="graph version not found") from exc
        except DeploymentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _summary(deployment, bootstrap.deployment)

    @app.post(
        "/deployments/{deployment_ref}/rollback",
        response_model=DeploymentSummaryResponse,
        status_code=201,
    )
    async def rollback_deployment(
        request: Request, deployment_ref: str, body: RollbackDeploymentRequest
    ) -> DeploymentSummaryResponse:
        """Create a new deployment version pinned to an earlier graph version."""
        principal = await require_permission(request, Permission.DEPLOYMENT_ADMIN)
        bootstrap = request.app.state.bootstrap
        try:
            deployment = await bootstrap.deployment_service.rollback(
                deployment_ref,
                target_graph_version=body.target_graph_version,
                tenant_id=principal.tenant_id,
                workspace_id=principal.workspace_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="deployment not found") from exc
        except DeploymentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _summary(deployment, bootstrap.deployment)
