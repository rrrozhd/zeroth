"""Strict configuration and tenant namespace guards for deployed acceptance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from release.gates.identity import identity_digest

_ROLES = frozenset({"operator", "reviewer", "admin"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RUN_ID = re.compile(r"^[a-z0-9]{8,64}$")


class LifecycleConfig(BaseModel):
    """Deployment-owned lifecycle endpoints required for durability evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    restart_url: str
    shutdown_url: str
    restart_status: int = Field(default=202, ge=200, le=299)
    shutdown_status: int = Field(default=202, ge=200, le=299)

    @field_validator("restart_url", "shutdown_url")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or parsed.scheme or parsed.netloc:
            raise ValueError("lifecycle URL must be an origin-relative path")
        return value


class AcceptanceConfig(BaseModel):
    """Non-secret configuration loaded before any network request is made."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    base_url: str
    tenant_id: str
    deployment_ref: str = Field(min_length=1)
    candidate_identity: Path
    credentials: dict[str, str]
    lifecycle: LifecycleConfig | None = None
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    poll_deadline_seconds: float = Field(default=60.0, gt=0, le=600)
    poll_interval_seconds: float = Field(default=0.1, gt=0, le=10)

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must use HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("base_url must identify the deployment origin root")
        return value.rstrip("/")

    @field_validator("tenant_id")
    @classmethod
    def _dedicated_tenant(cls, value: str) -> str:
        if value == "default":
            raise ValueError("a dedicated acceptance tenant is required")
        if not value.startswith("acceptance-"):
            raise ValueError("tenant_id must start with 'acceptance-'")
        if not re.fullmatch(r"[a-z0-9-]{12,80}", value):
            raise ValueError("tenant_id contains unsafe characters")
        return value

    @model_validator(mode="after")
    def _credential_references(self) -> AcceptanceConfig:
        if set(self.credentials) != _ROLES:
            raise ValueError("credentials must name operator, reviewer, and admin")
        references = list(self.credentials.values())
        if len(set(references)) != len(references):
            raise ValueError("role credentials must use distinct secret references")
        if not all(_ENV_NAME.fullmatch(reference) for reference in references):
            raise ValueError("credential values must be environment variable names")
        return self

    def resolve(
        self,
        environ: Mapping[str, str],
        *,
        run_id: str | None = None,
    ) -> ResolvedAcceptanceConfig:
        """Resolve secrets and candidate identity after structural validation."""
        secrets: dict[str, SecretStr] = {}
        for role, reference in self.credentials.items():
            value = environ.get(reference)
            if not value:
                raise ValueError(f"required credential environment variable {reference} is unset")
            secrets[role] = SecretStr(value)
        if len({secret.get_secret_value() for secret in secrets.values()}) != len(secrets):
            raise ValueError("operator, reviewer, and admin credentials must be distinct")
        try:
            candidate = json.loads(self.candidate_identity.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"candidate identity is unreadable: {error}") from error
        image = candidate.get("image") if isinstance(candidate, dict) else None
        if not isinstance(image, dict) or not image:
            raise ValueError("candidate identity must contain image identity")
        if not all(
            isinstance(reference, str)
            and reference
            and isinstance(digest, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            for reference, digest in image.items()
        ):
            raise ValueError("candidate image identity must contain immutable sha256 digests")
        identifier = run_id or uuid4().hex[:16]
        if not _RUN_ID.fullmatch(identifier):
            raise ValueError("run_id must be 8-64 lowercase alphanumeric characters")
        return ResolvedAcceptanceConfig(
            base_url=self.base_url,
            tenant_id=self.tenant_id,
            deployment_ref=self.deployment_ref,
            lifecycle=self.lifecycle,
            timeout_seconds=self.timeout_seconds,
            poll_deadline_seconds=self.poll_deadline_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            namespace=f"{self.tenant_id}-{identifier}",
            candidate_identity=candidate,
            candidate_digest=identity_digest(candidate),
            credentials=secrets,
        )


class ResolvedAcceptanceConfig(BaseModel):
    """Runtime configuration; credentials are excluded from serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    base_url: str
    tenant_id: str
    deployment_ref: str
    lifecycle: LifecycleConfig | None
    timeout_seconds: float
    poll_deadline_seconds: float
    poll_interval_seconds: float
    namespace: str
    candidate_identity: dict[str, Any]
    candidate_digest: str
    credentials: dict[str, SecretStr] = Field(exclude=True)

    def owns(self, identifier: str) -> bool:
        """Return whether an identifier belongs exclusively to this invocation."""
        return identifier == self.namespace or identifier.startswith(f"{self.namespace}-")

    def require_owned(self, identifier: str) -> None:
        """Refuse destructive work outside this invocation's namespace."""
        if not self.owns(identifier):
            raise ValueError(f"resource {identifier!r} is outside acceptance namespace")
