"""Public, HTTPX-compatible exceptions for the Zeroth SDK."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx

_MAX_DETAIL_LENGTH = 512
_MAX_ID_LENGTH = 128


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _sanitized(value: str, *, secrets: Iterable[str], limit: int) -> str:
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return _bounded(sanitized, limit)


def _response_payload(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _payload_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            if key in payload:
                value = payload[key]
                return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return None
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return json.dumps(payload, sort_keys=True)
    return None


def _payload_id(payload: Any, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None


class ZerothSDKError(Exception):
    """Base class for failures raised by the Zeroth SDK."""


class ZerothTransportError(httpx.RequestError, ZerothSDKError):
    """A sanitized network transport failure compatible with HTTPX catches."""

    def __init__(self, *, original_error: httpx.RequestError) -> None:
        self.original_error = original_error
        super().__init__(
            f"Zeroth transport request failed: {type(original_error).__name__}",
            request=original_error.request,
        )


class ZerothAPIError(httpx.HTTPStatusError, ZerothSDKError):
    """A sanitized non-success API response compatible with HTTPX catches."""

    def __init__(
        self,
        *,
        response: httpx.Response,
        detail: str,
        request_id: str | None,
        correlation_id: str | None,
    ) -> None:
        self.status_code = response.status_code
        self.detail = detail
        self.request_id = request_id
        self.correlation_id = correlation_id
        fields = [f"status={self.status_code}", f"detail={detail}"]
        if request_id is not None:
            fields.append(f"request_id={request_id}")
        if correlation_id is not None:
            fields.append(f"correlation_id={correlation_id}")
        super().__init__(
            "Zeroth API request failed: " + " ".join(fields),
            request=response.request,
            response=response,
        )


class ZerothAuthenticationError(ZerothAPIError):
    """The API rejected the caller's credentials with HTTP 401."""


class ZerothEntitlementError(ZerothAPIError):
    """The requested capability requires an unavailable entitlement (HTTP 402)."""


class ZerothAuthorizationError(ZerothAPIError):
    """The authenticated caller is not authorized (HTTP 403)."""


class ZerothNotFoundError(ZerothAPIError):
    """The tenant-scoped resource was not found (HTTP 404)."""


class ZerothConflictError(ZerothAPIError):
    """The request conflicts with current resource state (HTTP 409)."""


class ZerothValidationError(ZerothAPIError):
    """The API rejected request validation with HTTP 422."""


class ZerothRateLimitError(ZerothAPIError):
    """The API rate limit rejected the request with HTTP 429."""


class ZerothServerError(ZerothAPIError):
    """The API failed with an HTTP 5xx response."""


_STATUS_ERRORS: dict[int, type[ZerothAPIError]] = {
    401: ZerothAuthenticationError,
    402: ZerothEntitlementError,
    403: ZerothAuthorizationError,
    404: ZerothNotFoundError,
    409: ZerothConflictError,
    422: ZerothValidationError,
    429: ZerothRateLimitError,
}


def _api_error_from_response(
    response: httpx.Response, *, secrets: Iterable[str] = ()
) -> ZerothAPIError:
    """Build the typed sanitized exception for one non-success response."""
    payload = _response_payload(response)
    raw_detail = _payload_text(payload) or response.reason_phrase or "HTTP request failed"
    detail = _sanitized(raw_detail, secrets=secrets, limit=_MAX_DETAIL_LENGTH)
    raw_request_id = response.headers.get("x-request-id") or _payload_id(payload, "request_id")
    raw_correlation_id = response.headers.get("x-correlation-id") or _payload_id(
        payload, "correlation_id"
    )
    request_id = (
        _sanitized(raw_request_id, secrets=secrets, limit=_MAX_ID_LENGTH)
        if raw_request_id is not None
        else None
    )
    correlation_id = (
        _sanitized(raw_correlation_id, secrets=secrets, limit=_MAX_ID_LENGTH)
        if raw_correlation_id is not None
        else None
    )
    error_type = (
        ZerothServerError
        if response.status_code >= 500
        else _STATUS_ERRORS.get(response.status_code, ZerothAPIError)
    )
    return error_type(
        response=response,
        detail=detail,
        request_id=request_id,
        correlation_id=correlation_id,
    )


__all__ = [
    "ZerothAPIError",
    "ZerothAuthenticationError",
    "ZerothAuthorizationError",
    "ZerothConflictError",
    "ZerothEntitlementError",
    "ZerothNotFoundError",
    "ZerothRateLimitError",
    "ZerothSDKError",
    "ZerothServerError",
    "ZerothTransportError",
    "ZerothValidationError",
]
