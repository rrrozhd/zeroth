"""Shared backend primitives."""

from zeroth.platform.primitives.boundary import (
    OutboundDestinationError,
    ResolvedOutboundURL,
    confine_path,
    resolve_outbound_url,
    validate_outbound_url,
)
from zeroth.platform.primitives.clock import Clock, SystemClock, utc_now
from zeroth.platform.primitives.error_vocabulary import (
    ErrorCategory,
    categorize_exception,
    safe_error_detail,
)
from zeroth.platform.primitives.identifiers import new_uuid

__all__ = [
    "Clock",
    "ErrorCategory",
    "OutboundDestinationError",
    "ResolvedOutboundURL",
    "SystemClock",
    "categorize_exception",
    "confine_path",
    "new_uuid",
    "safe_error_detail",
    "resolve_outbound_url",
    "utc_now",
    "validate_outbound_url",
]
