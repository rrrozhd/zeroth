"""Authentication helpers for the deployment-bound service wrapper."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole

try:  # pragma: no cover - exercised once bearer verification lands
    import jwt
except ImportError:  # pragma: no cover - graceful until dependency is added
    jwt = None


_REMOTE_JWKS_MAX_BYTES = 64 * 1024
_REMOTE_JWKS_CACHE_SECONDS = 300.0
_REMOTE_JWKS_REFRESH_COOLDOWN_SECONDS = 5.0
_REDACTED_CONFIG_VALUE = "[redacted]"
_REDACTED_CONFIG_LOCATION = "[redacted-field]"
_REDACTED_CONFIG_KEY = "[redacted-key]"
_PYDANTIC_MAPPING_KEY_LOCATION_MARKER = "[key]"
_SAFE_CONFIG_LOCATION_SEGMENTS = frozenset(
    {
        "api_keys",
        "bearer",
        "custom_roles",
        "revoked_credential_ids",
        "credential_id",
        "secret",
        "subject",
        "roles",
        "tenant_id",
        "workspace_id",
        "issuer",
        "audience",
        "jwks_url",
        "jwks",
        "algorithms",
    }
)
_SAFE_ENUM_EXPECTED = "'operator', 'reviewer', 'admin' or 'platform_admin'"
_SAFE_CONFIG_VALUE_ERROR_MESSAGES = frozenset(
    {
        "credential_id must be non-empty",
        "static API key credential IDs must be unique",
        "static API key secrets must be unique",
        "revoked credential identifiers must be an array of strings",
        "revoked credential identifiers must be non-empty strings",
        "revoked credential identifiers must be unique",
        "bearer auth requires jwks_url or jwks",
    }
)


class _HTTPOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme.lower() not in {"http", "https"}:
            raise HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open_remote_jwks = build_opener(_HTTPOnlyRedirectHandler()).open


class AuthenticationError(RuntimeError):
    """Raised when a request cannot be authenticated."""


class CredentialStatusProvider(Protocol):
    """Synchronous source of credential status checked after verification."""

    def is_revoked(self, identifier: str) -> bool:
        """Whether a verified credential identifier has been revoked."""


def _identifier_snapshot(identifiers: Iterable[str]) -> frozenset[str]:
    """Validate and freeze one revocation identifier collection."""
    if isinstance(identifiers, (str, bytes)):
        raise ValueError("credential identifiers must be an iterable of non-empty strings")
    try:
        values = tuple(identifiers)
    except TypeError as exc:
        raise ValueError("credential identifiers must be an iterable of non-empty strings") from exc
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in values):
        raise ValueError("credential identifiers must be non-empty strings")
    return frozenset(values)


def _redacted_config_validation_error(error: ValidationError, *, title: str) -> ValidationError:
    """Build a type-valid config error without retaining caller-controlled data."""
    try:
        details = [_redacted_config_error_detail(detail) for detail in error.errors()]
        return ValidationError.from_exception_data(title, details)
    except Exception:
        # Do not let reconstruction failures expose the original validation error.
        return ValidationError.from_exception_data(
            title,
            [
                {
                    "type": "value_error",
                    "loc": (),
                    "input": _REDACTED_CONFIG_VALUE,
                    "ctx": {"error": ValueError("invalid configuration")},
                }
            ],
        )


def _redacted_config_error_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one Pydantic error detail using only fixed schema data."""
    error_type = detail["type"]
    location = tuple(detail.get("loc", ()))
    safe_detail: dict[str, Any] = {
        "type": error_type,
        "loc": tuple(
            _redacted_config_location_segment(location, index, error_type)
            for index in range(len(location))
        ),
        "input": _REDACTED_CONFIG_VALUE,
    }
    if error_type == "enum":
        expected = detail.get("ctx", {}).get("expected")
        safe_detail["ctx"] = {
            "expected": expected if expected == _SAFE_ENUM_EXPECTED else _REDACTED_CONFIG_VALUE
        }
    elif error_type == "value_error":
        message = str(detail.get("ctx", {}).get("error", ""))
        safe_detail["ctx"] = {
            "error": ValueError(
                message if message in _SAFE_CONFIG_VALUE_ERROR_MESSAGES else "invalid configuration"
            )
        }
    return safe_detail


def _redacted_config_location_segment(
    location: tuple[Any, ...], index: int, error_type: str
) -> str | int:
    """Preserve schema fields and list indices, never mapping keys from caller input."""
    segment = location[index]
    if error_type == "invalid_key":
        if segment in {
            _PYDANTIC_MAPPING_KEY_LOCATION_MARKER,
            _REDACTED_CONFIG_KEY,
        } or segment in _SAFE_CONFIG_LOCATION_SEGMENTS:
            return segment
        return _REDACTED_CONFIG_KEY
    if index + 1 < len(location) and location[index + 1] == _PYDANTIC_MAPPING_KEY_LOCATION_MARKER:
        return _REDACTED_CONFIG_LOCATION
    if isinstance(segment, int):
        return segment
    if (
        segment == _PYDANTIC_MAPPING_KEY_LOCATION_MARKER
        or segment in _SAFE_CONFIG_LOCATION_SEGMENTS
    ):
        return segment
    return _REDACTED_CONFIG_LOCATION


class CredentialRevocationRegistry:
    """Thread-safe revocations with linearizable snapshot replacement.

    A replacement linearizes when it acquires the decision lock. Callers that
    hold :meth:`decision_guard` make one status decision against one immutable
    snapshot; ``replace_snapshot`` cannot return until those decisions finish.
    """

    def __init__(self, revoked_credential_ids: Iterable[str] = ()) -> None:
        self._lock = threading.RLock()
        self._snapshot = _identifier_snapshot(revoked_credential_ids)

    @contextmanager
    def decision_guard(self) -> Iterator[None]:
        """Serialize a complete authentication status decision with replacement."""
        with self._lock:
            yield

    def is_revoked(self, identifier: str) -> bool:
        with self.decision_guard():
            return identifier in self._snapshot

    @property
    def snapshot(self) -> frozenset[str]:
        """Immutable current revocation view, safe to inspect or replace from bootstrap code."""
        with self._lock:
            return self._snapshot

    def replace_snapshot(self, identifiers: Iterable[str]) -> None:
        """Atomically replace the complete revoked-credential identifier set."""
        snapshot = _identifier_snapshot(identifiers)
        with self._lock:
            self._snapshot = snapshot


class StaticApiKeyCredential(BaseModel):
    """Static API key credential for service authentication."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    credential_id: str
    secret: str
    subject: str
    roles: list[ServiceRole] = Field(default_factory=list)
    tenant_id: str = "default"
    workspace_id: str | None = None

    def __init__(self, /, **data: Any) -> None:
        redacted_error: ValidationError | None = None
        try:
            super().__init__(**data)
        except ValidationError as exc:
            redacted_error = _redacted_config_validation_error(exc, title="StaticApiKeyCredential")
        if redacted_error is not None:
            raise redacted_error

    @field_validator("credential_id")
    @classmethod
    def _require_credential_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("credential_id must be non-empty")
        return value


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

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_keys: list[StaticApiKeyCredential] = Field(default_factory=list)
    bearer: BearerTokenConfig | None = None
    custom_roles: dict[str, list[str]] = Field(default_factory=dict)
    revoked_credential_ids: frozenset[str] = Field(default_factory=frozenset)

    def __init__(self, /, **data: Any) -> None:
        redacted_error: ValidationError | None = None
        try:
            super().__init__(**data)
        except ValidationError as exc:
            redacted_error = _redacted_config_validation_error(exc, title="ServiceAuthConfig")
        if redacted_error is not None:
            raise redacted_error

    @field_validator("revoked_credential_ids", mode="before")
    @classmethod
    def _validate_revoked_credential_ids(cls, value: object) -> frozenset[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("revoked credential identifiers must be an array of strings")
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in value):
            raise ValueError("revoked credential identifiers must be non-empty strings")
        identifiers = frozenset(value)
        if len(identifiers) != len(value):
            raise ValueError("revoked credential identifiers must be unique")
        return identifiers

    @model_validator(mode="after")
    def _require_unique_api_keys(self) -> ServiceAuthConfig:
        credential_ids: set[str] = set()
        secrets: set[str] = set()
        for credential in self.api_keys:
            if credential.credential_id in credential_ids:
                raise ValueError("static API key credential IDs must be unique")
            if credential.secret in secrets:
                raise ValueError("static API key secrets must be unique")
            credential_ids.add(credential.credential_id)
            secrets.add(credential.secret)
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceAuthConfig:
        source = dict(env or os.environ)
        payload: dict[str, Any] = {}
        if source.get("ZEROTH_SERVICE_API_KEYS_JSON"):
            payload["api_keys"] = json.loads(source["ZEROTH_SERVICE_API_KEYS_JSON"])
        if source.get("ZEROTH_SERVICE_BEARER_JSON"):
            payload["bearer"] = json.loads(source["ZEROTH_SERVICE_BEARER_JSON"])
        if source.get("ZEROTH_SERVICE_ROLES_JSON"):
            payload["custom_roles"] = json.loads(source["ZEROTH_SERVICE_ROLES_JSON"])
        if source.get("ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON"):
            payload["revoked_credential_ids"] = json.loads(
                source["ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON"]
            )
        return cls(**payload)


_auth_parameters = inspect.signature(ServiceAuthConfig).parameters
ServiceAuthConfig.__signature__ = inspect.signature(ServiceAuthConfig).replace(
    parameters=[
        parameter
        for name, parameter in _auth_parameters.items()
        if name not in {"custom_roles", "revoked_credential_ids"}
    ]
)


class JWTBearerTokenVerifier:
    """Verify JWT bearer tokens against issuer, audience, and JWKS metadata."""

    def __init__(self, config: BearerTokenConfig):
        self._config = config
        self._cached_jwks: tuple[float, dict[str, Any]] | None = None
        self._jwks_lock = threading.Lock()
        self._next_remote_fetch_at = 0.0

    def verify(self, token: str) -> AuthenticatedPrincipal:
        if jwt is None:
            raise AuthenticationError("invalid bearer token")
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:  # pragma: no cover - dependency-specific details
            raise AuthenticationError("invalid bearer token") from exc
        try:
            key = (
                self._resolve_signing_key(header.get("kid"), self._config.jwks)
                if self._config.jwks
                else self._resolve_remote_signing_key(header.get("kid"))
            )
            if key is None:
                raise AuthenticationError("invalid bearer token")
        except Exception as exc:  # pragma: no cover - dependency-specific details
            raise AuthenticationError("invalid bearer token") from exc
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._config.algorithms),
                issuer=self._config.issuer,
                audience=self._config.audience,
                options={"require": ["exp"]},
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

    def _resolve_remote_signing_key(self, kid: str | None) -> Any:
        with self._jwks_lock:
            cached_jwks = self._cached_jwks
            cached_remote_jwks = (
                cached_jwks
                if cached_jwks is not None and cached_jwks[0] > time.monotonic()
                else None
            )
            jwks = cached_remote_jwks[1] if cached_remote_jwks is not None else None
            if jwks is None:
                if time.monotonic() < self._next_remote_fetch_at:
                    return None
                try:
                    jwks = self._load_jwks()
                except Exception:
                    self._next_remote_fetch_at = (
                        time.monotonic() + _REMOTE_JWKS_REFRESH_COOLDOWN_SECONDS
                    )
                    raise
            key = self._resolve_signing_key(kid, jwks)
            if key is None and cached_remote_jwks is not None:
                if time.monotonic() < self._next_remote_fetch_at:
                    return None
                try:
                    jwks = self._load_jwks()
                finally:
                    self._next_remote_fetch_at = (
                        time.monotonic() + _REMOTE_JWKS_REFRESH_COOLDOWN_SECONDS
                    )
                key = self._resolve_signing_key(kid, jwks)
            return key

    def _load_jwks(self) -> dict[str, Any]:
        if urlsplit(self._config.jwks_url or "").scheme.lower() not in {"http", "https"}:
            raise ValueError("remote JWKS URL must use HTTP(S)")
        with _open_remote_jwks(
            self._config.jwks_url, timeout=3.0
        ) as response:  # pragma: no cover - network path
            payload = response.read(_REMOTE_JWKS_MAX_BYTES + 1)
        if len(payload) > _REMOTE_JWKS_MAX_BYTES:
            raise ValueError("remote JWKS response is too large")
        jwks = json.loads(payload.decode("utf-8"))
        if not isinstance(jwks, dict):
            raise ValueError("remote JWKS response is not an object")
        self._cached_jwks = (time.monotonic() + _REMOTE_JWKS_CACHE_SECONDS, jwks)
        return jwks

    def _resolve_signing_key(self, kid: str | None, jwks: dict[str, Any]) -> Any:
        if jwt is None:  # pragma: no cover - defensive guard
            raise AuthenticationError("invalid bearer token")
        jwk_set = jwt.PyJWKSet.from_dict(jwks)
        for jwk in jwk_set.keys:
            if kid is None or jwk.key_id == kid:
                return jwk.key
        return None


class ServiceAuthenticator:
    """Authenticate request headers into a shared principal shape."""

    def __init__(
        self,
        config: ServiceAuthConfig | None = None,
        *,
        bearer_verifier: JWTBearerTokenVerifier | None = None,
        credential_status_provider: CredentialStatusProvider | None = None,
    ) -> None:
        self._config = config or ServiceAuthConfig()
        self._api_keys: tuple[StaticApiKeyCredential, ...] = tuple(self._config.api_keys)
        self._bearer_verifier = bearer_verifier or (
            JWTBearerTokenVerifier(self._config.bearer) if self._config.bearer else None
        )
        self._credential_status_provider = (
            credential_status_provider
            if credential_status_provider is not None
            else CredentialRevocationRegistry(self._config.revoked_credential_ids)
        )

    @property
    def credential_status_provider(self) -> CredentialStatusProvider:
        """Provider used to make the final credential-status decision."""
        return self._credential_status_provider

    def _require_active_credential(self, identifier: str) -> None:
        provider = self._credential_status_provider
        if isinstance(provider, CredentialRevocationRegistry):
            with provider.decision_guard():
                if provider.is_revoked(identifier):
                    raise AuthenticationError("authentication required")
            return
        if provider.is_revoked(identifier):
            raise AuthenticationError("authentication required")

    def _match_api_key(self, presented: str) -> StaticApiKeyCredential | None:
        """Constant-time lookup of a stored API key credential by presented secret."""
        presented_bytes = presented.encode("utf-8")
        match: StaticApiKeyCredential | None = None
        for credential in self._api_keys:
            stored_bytes = credential.secret.encode("utf-8")
            if hmac.compare_digest(stored_bytes, presented_bytes) and match is None:
                match = credential
        return match

    def authenticate_headers(self, headers: Mapping[str, str]) -> AuthenticatedPrincipal:
        api_key = headers.get("X-API-Key")
        if api_key:
            credential = self._match_api_key(api_key)
            if credential is None:
                raise AuthenticationError("authentication required")
            self._require_active_credential(credential.credential_id)
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
            principal = self._bearer_verifier.verify(token)
            jti = principal.claims.get("jti") if principal.claims is not None else None
            identifier = (
                jti
                if isinstance(jti, str) and jti.strip()
                else "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
            )
            self._require_active_credential(identifier)
            return principal

        raise AuthenticationError("authentication required")


def current_principal(request: Request) -> AuthenticatedPrincipal:
    """Read the authenticated principal from request state."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise RuntimeError("request principal is not set")
    return principal


async def record_service_denial(
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
    await audit_repository.write(
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
            # Denials are terminal — stamp completed_at so the Audit view doesn't
            # render "Completed —" for forbidden / unauthenticated records.
            completed_at=datetime.now(UTC),
        )
    )
