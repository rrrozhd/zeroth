"""Authentication helpers for the deployment-bound service wrapper."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.request import urlopen
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.audit import AuditRepository, NodeAuditRecord
from zeroth.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole

try:  # pragma: no cover - exercised once bearer verification lands
    import jwt
except ImportError:  # pragma: no cover - graceful until dependency is added
    jwt = None


class AuthenticationError(RuntimeError):
    """Raised when a request cannot be authenticated."""


class StaticApiKeyCredential(BaseModel):
    """Static API key credential for service authentication."""

    model_config = ConfigDict(extra="forbid")

    credential_id: str
    secret: str
    subject: str
    roles: list[ServiceRole] = Field(default_factory=list)
    tenant_id: str = "default"
    workspace_id: str | None = None


class BearerTokenConfig(BaseModel):
    """JWT/OIDC verifier settings for bearer-token authentication."""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    audience: str
    jwks_url: str | None = None
    jwks: dict[str, Any] | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])

    @model_validator(mode="after")
    def _require_key_source(self) -> BearerTokenConfig:
        if self.jwks_url is None and self.jwks is None:
            raise ValueError("bearer auth requires jwks_url or jwks")
        return self


class ServiceAuthConfig(BaseModel):
    """Top-level service authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    api_keys: list[StaticApiKeyCredential] = Field(default_factory=list)
    bearer: BearerTokenConfig | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceAuthConfig:
        source = dict(env or os.environ)
        payload: dict[str, Any] = {}
        if source.get("ZEROTH_SERVICE_API_KEYS_JSON"):
            payload["api_keys"] = json.loads(source["ZEROTH_SERVICE_API_KEYS_JSON"])
        if source.get("ZEROTH_SERVICE_BEARER_JSON"):
            payload["bearer"] = json.loads(source["ZEROTH_SERVICE_BEARER_JSON"])
        return cls.model_validate(payload)


class JWTBearerTokenVerifier:
    """Verify JWT bearer tokens against issuer, audience, and JWKS metadata."""

    def __init__(self, config: BearerTokenConfig):
        self._config = config

    def verify(self, token: str) -> AuthenticatedPrincipal:
        if jwt is None:
            raise AuthenticationError("bearer auth dependency is not installed")
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:  # pragma: no cover - dependency-specific details
            raise AuthenticationError("invalid bearer token") from exc
        jwks = self._config.jwks or self._load_jwks()
        key = self._resolve_signing_key(header.get("kid"), jwks)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._config.algorithms),
                issuer=self._config.issuer,
                audience=self._config.audience,
            )
        except Exception as exc:  # pragma: no cover - dependency-specific details
            raise AuthenticationError("invalid bearer token") from exc
        return AuthenticatedPrincipal(
            subject=str(claims["sub"]),
            auth_method=AuthMethod.BEARER,
            roles=[ServiceRole(role) for role in claims.get("roles", [])],
            tenant_id=str(claims.get("tenant_id", "default")),
            workspace_id=claims.get("workspace_id"),
            claims=dict(claims),
        )

    def _load_jwks(self) -> dict[str, Any]:
        with urlopen(self._config.jwks_url) as response:  # pragma: no cover - network path
            return json.loads(response.read().decode("utf-8"))

    def _resolve_signing_key(self, kid: str | None, jwks: dict[str, Any]) -> Any:
        if jwt is None:  # pragma: no cover - defensive guard
            raise AuthenticationError("bearer auth dependency is not installed")
        jwk_set = jwt.PyJWKSet.from_dict(jwks)
        for jwk in jwk_set.keys:
            if kid is None or jwk.key_id == kid:
                return jwk.key
        raise AuthenticationError("invalid bearer token")


class ServiceAuthenticator:
    """Authenticate request headers into a shared principal shape."""

    def __init__(
        self,
        config: ServiceAuthConfig | None = None,
        *,
        bearer_verifier: JWTBearerTokenVerifier | None = None,
    ) -> None:
        self._config = config or ServiceAuthConfig()
        self._api_keys = {credential.secret: credential for credential in self._config.api_keys}
        self._bearer_verifier = bearer_verifier or (
            JWTBearerTokenVerifier(self._config.bearer) if self._config.bearer else None
        )

    def authenticate_headers(self, headers: Mapping[str, str]) -> AuthenticatedPrincipal:
        api_key = headers.get("X-API-Key")
        if api_key:
            credential = self._api_keys.get(api_key)
            if credential is None:
                raise AuthenticationError("authentication required")
            return AuthenticatedPrincipal(
                subject=credential.subject,
                auth_method=AuthMethod.API_KEY,
                roles=list(credential.roles),
                tenant_id=credential.tenant_id,
                workspace_id=credential.workspace_id,
                credential_id=credential.credential_id,
            )

        authorization = headers.get("Authorization", "")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationError("authentication required")
            if self._bearer_verifier is None:
                raise AuthenticationError("authentication required")
            return self._bearer_verifier.verify(token)

        raise AuthenticationError("authentication required")


def current_principal(request: Request) -> AuthenticatedPrincipal:
    """Read the authenticated principal from request state."""

    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise RuntimeError("request principal is not set")
    return principal


def record_service_denial(
    *,
    audit_repository: AuditRepository | None,
    deployment: object | None,
    request: Request,
    node_id: str,
    status: str,
    error: str,
    actor=None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an authentication or authorization denial via the audit repository."""

    if audit_repository is None:
        return
    tenant_id = getattr(deployment, "tenant_id", "default")
    workspace_id = getattr(deployment, "workspace_id", None)
    audit_repository.write(
        NodeAuditRecord(
            audit_id=f"{node_id}:{uuid4().hex}",
            run_id=f"service:{request.method}:{request.url.path}",
            thread_id=None,
            node_id=node_id,
            graph_version_ref=getattr(deployment, "graph_version_ref", "service"),
            deployment_ref=getattr(deployment, "deployment_ref", "service"),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            status=status,
            actor=actor,
            execution_metadata={
                "request": {
                    "method": request.method,
                    "path": request.url.path,
                },
                **dict(metadata or {}),
            },
            error=error,
            started_at=datetime.now(UTC),
        )
    )
