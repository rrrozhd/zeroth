"""Deployment listing REST API.

Surfaces deployments created through the medium-code path (code-defined
graphs deployed via :class:`DeploymentService`) so operator UIs — the
bundled console in particular — can display every persisted deployment
version alongside the one this service instance is currently serving.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict

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


def register_deployment_routes(app: FastAPI | APIRouter) -> None:
    """Register deployment listing routes on the FastAPI app."""

    @app.get("/deployments", response_model=list[DeploymentSummaryResponse])
    async def list_deployments(request: Request) -> list[DeploymentSummaryResponse]:
        """All persisted deployment versions, newest first.

        ``serving`` marks the deployment version this service instance was
        bootstrapped with (the one /health reports).
        """
        await require_permission(request, Permission.DEPLOYMENT_READ)
        bootstrap = request.app.state.bootstrap
        serving = bootstrap.deployment
        deployments = await bootstrap.deployment_service.list()
        deployments.sort(key=lambda d: d.created_at, reverse=True)
        return [
            DeploymentSummaryResponse(
                deployment_ref=d.deployment_ref,
                version=d.version,
                graph_version_ref=d.graph_version_ref,
                status=d.status.value,
                serving=(
                    d.deployment_ref == serving.deployment_ref
                    and d.version == serving.version
                ),
                created_at=d.created_at.isoformat(),
            )
            for d in deployments
        ]
