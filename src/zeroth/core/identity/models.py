"""Shared principal and actor identity models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuthMethod(StrEnum):
    """Supported request authentication methods."""

    API_KEY = "api_key"
    BEARER = "bearer"


class ServiceRole(StrEnum):
    """Built-in service roles used by route authorization.

    These three are always present. Deployments may define additional custom
    roles by name via configuration (``ZEROTH_SERVICE_ROLES_JSON``); principals
    carry role names as plain strings so a custom role flows through the same
    path, and an unknown name simply grants no permissions (fail-closed).
    """

    OPERATOR = "operator"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class PrincipalScope(BaseModel):
    """Tenant and workspace scope carried by principals and resources."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "default"
    workspace_id: str | None = None


class ActorIdentity(BaseModel):
    """Stable actor identity recorded on runs, approvals, and audits."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    auth_method: AuthMethod
    # Role names, not the ServiceRole enum: built-in roles serialize to the same
    # strings ("operator"/…), and custom role names defined via config flow
    # through unchanged. Permission resolution happens against the role registry.
    roles: list[str] = Field(default_factory=list)
    tenant_id: str = "default"
    workspace_id: str | None = None


class AuthenticatedPrincipal(ActorIdentity):
    """Authenticated request principal with request-only details."""

    credential_id: str | None = None
    claims: dict[str, Any] = Field(default_factory=dict)

    def scope(self) -> PrincipalScope:
        return PrincipalScope(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
        )

    def to_actor(self) -> ActorIdentity:
        return ActorIdentity(
            subject=self.subject,
            auth_method=self.auth_method,
            roles=list(self.roles),
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
        )
