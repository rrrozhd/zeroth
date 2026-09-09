"""HTTP client for Zeroth's hosted optimization API."""

from zeroth.sdk.client import ZerothClient
from zeroth.sdk.errors import (
    ZerothAPIError,
    ZerothAuthenticationError,
    ZerothAuthorizationError,
    ZerothConflictError,
    ZerothEntitlementError,
    ZerothNotFoundError,
    ZerothRateLimitError,
    ZerothSDKError,
    ZerothServerError,
    ZerothTransportError,
    ZerothValidationError,
)

__all__ = [
    "ZerothAPIError",
    "ZerothAuthenticationError",
    "ZerothAuthorizationError",
    "ZerothClient",
    "ZerothConflictError",
    "ZerothEntitlementError",
    "ZerothNotFoundError",
    "ZerothRateLimitError",
    "ZerothSDKError",
    "ZerothServerError",
    "ZerothTransportError",
    "ZerothValidationError",
]
