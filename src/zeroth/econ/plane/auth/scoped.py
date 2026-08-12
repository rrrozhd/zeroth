"""Versioned authentication contracts carrying trusted persistence scope."""

from pydantic import BaseModel, EmailStr


class ScopedLoginRequest(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]
    tenant_id: str = "default"
    workspace_id: str | None = None


class ScopedUserClaims(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]
    tenant_id: str
    workspace_id: str | None = None
    exp: int
    iss: str
