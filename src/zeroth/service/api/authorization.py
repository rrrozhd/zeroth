"""Authorization helpers for the deployment-bound service routes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from fastapi import HTTPException, Request, status

from zeroth.governance.identity import AuthenticatedPrincipal, ServiceRole
from zeroth.service.api.authentication import current_principal, record_service_denial


class Permission(StrEnum):
    """Permissions enforced by the Phase 6 service surface."""

    DEPLOYMENT_READ = "deployment:read"
    DEPLOYMENT_ADMIN = "deployment:admin"
    RUN_CREATE = "run:create"
    RUN_READ = "run:read"
    APPROVAL_READ = "approval:read"
    APPROVAL_RESOLVE = "approval:resolve"
    AUDIT_READ = "audit:read"
    RUN_ADMIN = "run:admin"
    METRICS_READ = "metrics:read"
    METRICS_ADMIN = "metrics:admin"
    WEBHOOK_ADMIN = "webhook:admin"
    TEMPLATE_ADMIN = "template:admin"
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_ADMIN = "workflow:admin"
    CONNECTOR_ADMIN = "connector:admin"
    # WS-E: retention policy, legal-hold, and right-to-erasure administration.
    # Deliberately admin-tier (ADMIN holds all permissions); erasure is
    # irreversible and destroys the plaintext behind the audit trail.
    RETENTION_ADMIN = "retention:admin"
    # ZER-8: the tool-enforcement surface an SDK adapter talks to -- ask for a
    # decision, declare a tool inventory, attest a run, report liveness, read
    # back the level the server computed. Deliberately NOT admin-tier: the
    # caller is a running deployment, not an operator, and every route is
    # scoped to the caller's own tenant and deployment. It grants no ability to
    # raise a governance level -- the server recomputes that from state it
    # holds -- so the blast radius is writing evidence about oneself.
    ENFORCEMENT_REPORT = "enforcement:report"
    # Global economic-control-plane mutations are deliberately excluded from
    # tenant/deployment administrators.
    ECON_ADMIN = "econ:admin"


ROLE_PERMISSIONS: dict[ServiceRole, set[Permission]] = {
    ServiceRole.OPERATOR: {
        Permission.DEPLOYMENT_READ,
        # Deploying a published graph is the tail of the operator authoring
        # loop (author -> publish -> deploy); serving swaps remain an ops
        # action (restart with the new deployment_ref).
        Permission.DEPLOYMENT_ADMIN,
        Permission.RUN_CREATE,
        Permission.RUN_READ,
        Permission.APPROVAL_READ,
        # Operators author graphs (the medium-code build path). Cost/spend
        # (METRICS_READ) stays admin-only, consistent with the /metrics endpoint.
        Permission.WORKFLOW_READ,
        Permission.WORKFLOW_ADMIN,
        # Operators author graphs and the infrastructure bindings those
        # graphs depend on -- memory connector CRUD is part of that
        # authoring surface. Reviewers stay read-only.
        Permission.CONNECTOR_ADMIN,
        # A deployment reporting its own enforcement evidence sits at the same
        # tier as starting a run (RUN_CREATE): it is the running system talking
        # about itself, not an operator changing what is governed.
        Permission.ENFORCEMENT_REPORT,
    },
    ServiceRole.REVIEWER: {
        Permission.DEPLOYMENT_READ,
        Permission.RUN_READ,
        Permission.APPROVAL_READ,
        Permission.APPROVAL_RESOLVE,
        # Reviewers see graphs but don't author them.
        Permission.WORKFLOW_READ,
        # Reviewing evidence is the role's purpose: the audit trail is
        # read-only governance data, same tier as runs and approvals.
        # Cost/spend (METRICS_READ) stays admin-only.
        Permission.AUDIT_READ,
    },
    ServiceRole.ADMIN: set(Permission) - {Permission.ECON_ADMIN},
    ServiceRole.PLATFORM_ADMIN: set(Permission),
}

BUILTIN_ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    role.value: frozenset(permissions) for role, permissions in ROLE_PERMISSIONS.items()
}


class RoleRegistry:
    """Resolve built-in and configured role names to permission sets."""

    def __init__(self, custom_roles: Mapping[str, Iterable[Permission]] | None = None) -> None:
        table = dict(BUILTIN_ROLE_PERMISSIONS)
        for name, permissions in (custom_roles or {}).items():
            if name in BUILTIN_ROLE_PERMISSIONS:
                raise ValueError(f"custom role {name!r} collides with a built-in role")
            table[name] = frozenset(permissions)
        self._table = table

    @classmethod
    def from_config(cls, custom_roles: Mapping[str, Iterable[str]] | None) -> RoleRegistry:
        resolved: dict[str, set[Permission]] = {}
        for name, permission_names in (custom_roles or {}).items():
            permissions: set[Permission] = set()
            for permission_name in permission_names:
                try:
                    permissions.add(Permission(permission_name))
                except ValueError as exc:
                    raise ValueError(
                        f"custom role {name!r} references unknown permission {permission_name!r}"
                    ) from exc
            resolved[name] = permissions
        return cls(resolved)

    def permissions_for(self, roles: Iterable[str]) -> set[Permission]:
        allowed: set[Permission] = set()
        for role in roles:
            allowed.update(self._table.get(role, ()))
        return allowed

    def known_roles(self) -> frozenset[str]:
        return frozenset(self._table)


DEFAULT_ROLE_REGISTRY = RoleRegistry()


def _role_registry(request: Request) -> RoleRegistry:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    registry = getattr(bootstrap, "role_registry", None)
    return registry if isinstance(registry, RoleRegistry) else DEFAULT_ROLE_REGISTRY


async def require_permission(request: Request, permission: Permission) -> AuthenticatedPrincipal:
    """Require that the authenticated principal holds the requested permission."""
    principal = current_principal(request)
    allowed = _role_registry(request).permissions_for(principal.roles)
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
    if principal.tenant_id == deployment_tenant and principal.workspace_id == deployment_workspace:
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
