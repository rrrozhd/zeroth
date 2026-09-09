"""Shared backend primitives."""

from zeroth.platform.primitives.boundary import (
    DestinationNotADirectoryError,
    OutboundDestinationError,
    ResolvedOutboundURL,
    confine_directory,
    confine_path,
    resolve_outbound_url,
    validate_outbound_url,
)
from zeroth.platform.primitives.cancellation import (
    cleanup_despite_cancellation,
    finish_despite_cancellation,
    finish_in_thread,
)
from zeroth.platform.primitives.clock import Clock, SystemClock, utc_now
from zeroth.platform.primitives.error_vocabulary import (
    ErrorCategory,
    categorize_exception,
    safe_error_detail,
)
from zeroth.platform.primitives.identifiers import new_uuid
from zeroth.platform.primitives.safe_yaml import (
    UntrustedYamlError,
    UntrustedYamlErrorCode,
    load_untrusted_yaml,
)

__all__ = [
    "Clock",
    "DestinationNotADirectoryError",
    "ErrorCategory",
    "OutboundDestinationError",
    "ResolvedOutboundURL",
    "SystemClock",
    "UntrustedYamlError",
    "UntrustedYamlErrorCode",
    "categorize_exception",
    "cleanup_despite_cancellation",
    "confine_directory",
    "confine_path",
    "finish_despite_cancellation",
    "finish_in_thread",
    "load_untrusted_yaml",
    "new_uuid",
    "safe_error_detail",
    "resolve_outbound_url",
    "utc_now",
    "validate_outbound_url",
]
