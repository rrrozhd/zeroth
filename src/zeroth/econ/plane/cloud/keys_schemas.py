"""Project API key creation and metadata contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ALLOWED_ROLES = {"Admin", "Analyst", "Approver", "Viewer"}


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=lambda: ["Analyst"], min_length=1)

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: list[str]) -> list[str]:
        unknown = set(value) - _ALLOWED_ROLES
        if unknown:
            raise ValueError(f"unknown roles: {', '.join(sorted(unknown))}")
        return list(dict.fromkeys(value))


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key_id: str
    name: str
    last_four: str
    roles: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyReveal(ApiKeyOut):
    api_key: str
