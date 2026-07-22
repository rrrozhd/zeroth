from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserClaims(BaseModel):
    sub: str
    email: EmailStr
    roles: list[str]
    exp: int
    iss: str
