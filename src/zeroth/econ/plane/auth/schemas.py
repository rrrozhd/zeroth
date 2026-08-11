from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]
    tenant_id: str = "default"
    workspace_id: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserClaims(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]
    tenant_id: str
    workspace_id: str | None = None
    exp: int
    iss: str
