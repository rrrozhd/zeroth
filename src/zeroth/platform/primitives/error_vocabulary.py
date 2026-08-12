"""What a caller learns when a backend call fails, from a set this codebase closed.

Several HTTP handlers reported a dependency failure by interpolating the caught
exception straight into a response body -- ``detail=str(exc)``,
``f"{type(exc).__name__}: {exc}"``, ``f"Regulus backend error: {exc}"``. The text
of a driver's exception is not written by this codebase: a redis client says which
host and port it could not reach, an SQLAlchemy ``OperationalError`` carries the
DSN it was constructed from, and an httpx error carries the full URL it dialled.
On ``/health/ready``, which answers before authentication, that reaches an
unauthenticated caller -- and it does so inside a **200** response, so a check
scoped to error status codes would not see it.

The failure still has to be legible: an operator reading a readiness probe needs
to know whether the database is unreachable, timing out, or refusing credentials,
and "an error occurred" tells them nothing. So this module does what
:mod:`zeroth.governance.audit.capture_vocabulary` does for audit metadata --
keeps the *category* and drops the *text*. The category is derived from the
exception's class name, never its message, because a class name is chosen by
whoever wrote the class and a message is assembled at raise time out of whatever
the raiser was holding.

Resolution walks the exception's MRO, so a driver-specific subclass of a
registered base (``redis.ConnectionError`` under ``ConnectionError``) resolves
without being enumerated. An unrecognised failure becomes
:data:`ErrorCategory.INTERNAL_ERROR` -- fail-closed by construction: forgetting to
register a class loses precision, never confidentiality.

This is deliberately **not** shared with the audit vocabulary. That one lives in
``governance``, which ``econ`` may not import (see ``zeroth/_architecture.py``),
and it answers a different question -- which *reason codes this codebase mints*
may be retained -- against a much larger closed set. This one answers what a
*third-party driver failure* may be reported as, in seven terms, to an
unauthenticated HTTP caller.
"""

from __future__ import annotations

import re
from enum import StrEnum

__all__ = [
    "ErrorCategory",
    "categorize_exception",
    "safe_error_detail",
]


class ErrorCategory(StrEnum):
    """The closed set of things a caller may learn about a backend failure.

    Chosen to be the distinctions an operator acts on differently: a host that
    cannot be reached, one that is reachable but slow, one that rejects the
    credential, a missing object, a configuration the client refused to accept,
    a dependency that is not configured at all, and everything else.
    """

    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    AUTHENTICATION_FAILED = "authentication_failed"
    NOT_FOUND = "not_found"
    INVALID_CONFIGURATION = "invalid_configuration"
    UNAVAILABLE = "unavailable"
    INTERNAL_ERROR = "internal_error"


def _normalize(name: str) -> str:
    """Render a class name as a stable ``snake_case`` lookup key."""
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"[^a-z0-9]+", "_", split.lower()).strip("_")


# Exception class names, normalized, mapped to what a caller may be told. Keys
# are matched against every class in the raised exception's MRO, so registering a
# base covers the driver subclasses that derive from it -- ``redis.ConnectionError``
# and ``httpx.ConnectError`` both resolve through entries here.
#
# Adding a driver means adding its class names here. That is the same deliberate
# edit that guards the set, and the default below makes forgetting safe.
_CATEGORY_BY_CLASS_NAME: dict[str, ErrorCategory] = {
    # Reachability. Builtins first, then the driver-specific names that do not
    # derive from them.
    "connection_error": ErrorCategory.UNREACHABLE,
    "connection_refused_error": ErrorCategory.UNREACHABLE,
    "connection_reset_error": ErrorCategory.UNREACHABLE,
    "connection_aborted_error": ErrorCategory.UNREACHABLE,
    "broken_pipe_error": ErrorCategory.UNREACHABLE,
    "connect_error": ErrorCategory.UNREACHABLE,
    "network_error": ErrorCategory.UNREACHABLE,
    "transport_error": ErrorCategory.UNREACHABLE,
    "gaierror": ErrorCategory.UNREACHABLE,
    "socket_error": ErrorCategory.UNREACHABLE,
    "disconnected_error": ErrorCategory.UNREACHABLE,
    "operational_error": ErrorCategory.UNREACHABLE,
    "interface_error": ErrorCategory.UNREACHABLE,
    # Timing.
    "timeout_error": ErrorCategory.TIMEOUT,
    "timeout_exception": ErrorCategory.TIMEOUT,
    "connect_timeout": ErrorCategory.TIMEOUT,
    "read_timeout": ErrorCategory.TIMEOUT,
    "pool_timeout": ErrorCategory.TIMEOUT,
    "write_timeout": ErrorCategory.TIMEOUT,
    "cancelled_error": ErrorCategory.TIMEOUT,
    # Credentials.
    "authentication_error": ErrorCategory.AUTHENTICATION_FAILED,
    "authorization_error": ErrorCategory.AUTHENTICATION_FAILED,
    "auth_error": ErrorCategory.AUTHENTICATION_FAILED,
    "permission_error": ErrorCategory.AUTHENTICATION_FAILED,
    "no_permission_error": ErrorCategory.AUTHENTICATION_FAILED,
    "invalid_token_error": ErrorCategory.AUTHENTICATION_FAILED,
    # Missing objects.
    "key_error": ErrorCategory.NOT_FOUND,
    "file_not_found_error": ErrorCategory.NOT_FOUND,
    "not_found_error": ErrorCategory.NOT_FOUND,
    "no_such_module_error": ErrorCategory.NOT_FOUND,
    # Configuration the client itself refused.
    "value_error": ErrorCategory.INVALID_CONFIGURATION,
    "type_error": ErrorCategory.INVALID_CONFIGURATION,
    "validation_error": ErrorCategory.INVALID_CONFIGURATION,
    "data_error": ErrorCategory.INVALID_CONFIGURATION,
    "argument_error": ErrorCategory.INVALID_CONFIGURATION,
    "outbound_destination_error": ErrorCategory.INVALID_CONFIGURATION,
    "invalid_response": ErrorCategory.INVALID_CONFIGURATION,
    # Dependency absent rather than broken.
    "import_error": ErrorCategory.UNAVAILABLE,
    "module_not_found_error": ErrorCategory.UNAVAILABLE,
    "not_implemented_error": ErrorCategory.UNAVAILABLE,
    "busy_loading_error": ErrorCategory.UNAVAILABLE,
}


def categorize_exception(exc: BaseException) -> ErrorCategory:
    """Resolve ``exc`` to the one category a caller may be told about.

    Walks the MRO most-derived first, so a driver's own subclass resolves through
    whichever registered base it derives from. Nothing about the exception's
    *message* is consulted.
    """
    for klass in type(exc).__mro__:
        category = _CATEGORY_BY_CLASS_NAME.get(_normalize(klass.__name__))
        if category is not None:
            return category
    return ErrorCategory.INTERNAL_ERROR


def safe_error_detail(exc: BaseException, *, context: str) -> str:
    """Render ``exc`` as a response-safe detail string.

    Args:
        exc: The caught exception. Its message is never read.
        context: What was being attempted, chosen by the *calling code* -- e.g.
            ``"database"`` or ``"regulus backend"``. Never interpolate anything
            caller-supplied into it, or this function's whole purpose is undone.

    Returns:
        ``"<context>: <category>"``, drawn entirely from values this codebase
        chose.
    """
    return f"{context}: {categorize_exception(exc).value}"
