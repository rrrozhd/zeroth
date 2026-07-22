"""Legacy import path for :mod:`zeroth.platform.storage.database`."""

from zeroth.platform.storage.database import (
    DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    AsyncConnection,
    AsyncDatabase,
    CoordinationTimeoutError,
    validate_coordination_timeout,
)

__all__ = [
    "DEFAULT_COORDINATION_TIMEOUT_SECONDS",
    "AsyncConnection",
    "AsyncDatabase",
    "CoordinationTimeoutError",
    "validate_coordination_timeout",
]
