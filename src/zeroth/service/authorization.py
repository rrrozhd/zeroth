"""Authorization helpers for the deployment-bound service routes."""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, Request, status

from zeroth.identity import AuthenticatedPrincipal, ServiceRole
from zeroth.service.auth import current_principal, record_service_denial


class Permission(StrEnum):
    """Permissions enforced by the Phase 6 service surface."""

    DEPLOYMENT_READ = "deployment:read"
    RUN_CREATE = "run:create"
    RUN_READ = "run:read"
    APPROVAL_READ = "approval:read"
    APPROVAL_RESOLVE = "approval:resolve"
    AUDIT_READ = "audit:read"
    RUN_ADMIN = "run:admin"
    METRICS_READ = "metrics:read"


ROLE_PERMISSIONS: dict[ServiceRole, set[Permission]] = {
    ServiceRole.OPERATOR: {
        Permission.DEPLOYMENT_READ,
        Permission.RUN_CREATE,
        Permission.RUN_READ,
        Permission.APPROVAL_READ,
    },
    ServiceRole.REVIEWER: {
        Permission.DEPLOYMENT_READ,
        Permission.RUN_READ,
        Permission.APPROVAL_READ,
        Permission.APPROVAL_RESOLVE,
    },
    ServiceRole.ADMIN: set(Permission),
}


async def require_permission(request: Request, permission: Permission) -> AuthenticatedPrincipal:
    """Require that the authenticated principal holds the requested permission."""

    principal = current_principal(request)
    allowed = set().union(*(ROLE_PERMISSIONS.get(role, set()) for role in principal.roles))
    if permission in allowed:
        return principal
    bootstrap = getattr(request.app.state, "bootstrap", None)
    await record_service_denial(
        audit_repository=getattr(bootstrap, "audit_repository", None),
        deployment=getattr(bootstrap, "deployment", None),
        request=request,
        node_id="service.authorization",
        status="forbidden",
        error=f"missing permission {permission.value}",
        actor=principal.to_actor(),
        metadata={"permission": permission.value},
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


async def require_deployment_scope(
    request: Request,
    deployment: object,
    *,
    hide_as_not_found: bool = True,
) -> AuthenticatedPrincipal:
    """Require that the authenticated principal can access the deployment scope."""

    principal = current_principal(request)
    deployment_tenant = getattr(deployment, "tenant_id", "default")
    deployment_workspace = getattr(deployment, "workspace_id", None)
    if (
        principal.tenant_id == deployment_tenant
        and principal.workspace_id == deployment_workspace
    ):
        return principal
    bootstrap = getattr(request.app.state, "bootstrap", None)
    await record_service_denial(
        audit_repository=getattr(bootstrap, "audit_repository", None),
        deployment=deployment,
        request=request,
        node_id="service.authorization",
        status="forbidden",
        error="scope mismatch",
        actor=principal.to_actor(),
        metadata={
            "principal_tenant_id": principal.tenant_id,
            "principal_workspace_id": principal.workspace_id,
            "deployment_tenant_id": deployment_tenant,
            "deployment_workspace_id": deployment_workspace,
        },
    )
    if hide_as_not_found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


async def require_resource_scope(
    request: Request,
    *,
    tenant_id: str,
    workspace_id: str | None,
    not_found_detail: str,
) -> AuthenticatedPrincipal:
    """Require that the authenticated principal can access a scoped resource."""

    principal = current_principal(request)
    if principal.tenant_id == tenant_id and principal.workspace_id == workspace_id:
        return principal
    bootstrap = getattr(request.app.state, "bootstrap", None)
    await record_service_denial(
        audit_repository=getattr(bootstrap, "audit_repository", None),
        deployment=getattr(bootstrap, "deployment", None),
        request=request,
        node_id="service.authorization",
        status="forbidden",
        error="scope mismatch",
        actor=principal.to_actor(),
        metadata={
            "principal_tenant_id": principal.tenant_id,
            "principal_workspace_id": principal.workspace_id,
            "resource_tenant_id": tenant_id,
            "resource_workspace_id": workspace_id,
        },
    )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found_detail)
