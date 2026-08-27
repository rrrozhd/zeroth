"""Authenticated console identity and scope discovery."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from zeroth.service.api.authorization import Permission, require_permission


class IdentityResponse(BaseModel):
    """The scope and roles actually carried by the presented credential."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    roles: list[str]
    tenant_id: str
    workspace_id: str | None = None


def register_identity_routes(app: FastAPI | APIRouter) -> None:
    @app.get("/identity", response_model=IdentityResponse)
    async def get_identity(request: Request) -> IdentityResponse:
        principal = await require_permission(request, Permission.DEPLOYMENT_READ)
        return IdentityResponse(
            subject=principal.subject,
            roles=[str(role.value) for role in principal.roles],
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
