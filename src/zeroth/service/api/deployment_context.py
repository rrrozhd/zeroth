"""Tenant/workspace-safe deployment lookup for control-plane detail routes."""

from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException, Request, status

from zeroth.governance.identity import AuthenticatedPrincipal
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)


class DeploymentContextBootstrapLike(Protocol):
    """Bootstrap surface required to resolve a deployment registry entry."""

    deployment_service: object


async def require_scoped_deployment(
    request: Request,
    deployment_ref: str,
    permission: Permission,
) -> tuple[DeploymentContextBootstrapLike, object, AuthenticatedPrincipal]:
    """Authorize and resolve a deployment within the caller's exact scope.

    The runtime is bound to one deployment for execution, while the control
    plane lists every deployment in the caller's tenant/workspace.  Detail
    reads therefore resolve through the same repository-backed scope as the
    list instead of comparing the path ref to the process-bound deployment.
    """
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    principal = await require_permission(
        request,
        permission,
        enforce_deployment_scope=False,
    )
    deployment = await bootstrap.deployment_service.get(
        deployment_ref,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    await require_deployment_scope(request, deployment)
    return bootstrap, deployment, principal
