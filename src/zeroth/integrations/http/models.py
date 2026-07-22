"""HTTP client data models — settings, endpoint config, call records, helpers."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict

# HttpClientSettings is composed into ZerothSettings as a field, so its
# definition lives in the platform layer; this integrations path republishes
# the same class.
from zeroth.platform.config.models import HttpClientSettings

__all__ = [
    "AuthType",
    "EndpointConfig",
    "HttpCallRecord",
    "HttpClientSettings",
    "redact_url",
]


class AuthType(StrEnum):
    """Authentication scheme for an endpoint."""

    BEARER = "bearer"
    API_KEY = "api_key"
    CUSTOM_HEADER = "custom_header"


class EndpointConfig(BaseModel):
    """Per-endpoint overrides.  ``None`` means *use the global default*."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int | None = None
    retryable_status_codes: set[int] | None = None
    timeout: float | None = None
    secret_key: str | None = None
    auth_type: AuthType | None = None
    auth_header_name: str = "Authorization"
    rate_limit_rate: float | None = None
    rate_limit_burst: int | None = None


class HttpCallRecord(BaseModel):
    """Immutable record of a single outbound HTTP call for audit logging."""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: str
    status_code: int | None = None
    latency_ms: float
    response_size_bytes: int | None = None
    retry_count: int = 0
    circuit_breaker_state: str | None = None
    error: str | None = None


def redact_url(url: str) -> str:
    """Return *url* with query parameters and fragment stripped."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))
