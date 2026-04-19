"""ContextVar-based correlation ID propagation.

A correlation ID is set on each incoming HTTP request and propagated through
async code via Python's contextvars.  Log entries, metrics, and audit records
can include it for cross-component tracing.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async context."""
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Return the correlation ID for the current async context (empty string if unset)."""
    return _correlation_id.get()


def new_correlation_id() -> str:
    """Generate a new random correlation ID and set it as the current."""
    cid = uuid4().hex
    _correlation_id.set(cid)
    return cid
