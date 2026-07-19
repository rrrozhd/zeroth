"""Abstract async database protocol for all Zeroth repositories.

Defines the AsyncDatabase and AsyncConnection protocols that both
the SQLite and Postgres backends implement.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol, runtime_checkable

DEFAULT_COORDINATION_TIMEOUT_SECONDS = 5.0


class CoordinationTimeoutError(TimeoutError):
    """Raised when a database coordination lock cannot be acquired in time."""


def validate_coordination_timeout(timeout_seconds: float) -> float:
    """Return a finite positive coordination timeout or reject invalid input."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("coordination_timeout_seconds must be finite positive")
    return timeout_seconds


@runtime_checkable
class AsyncConnection(Protocol):
    """Abstraction over a database connection within a transaction."""

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None: ...

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None: ...

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...

    async def execute_script(self, sql: str) -> None: ...


@runtime_checkable
class AsyncDatabase(Protocol):
    """Abstract async database interface for all repositories."""

    backend: Literal["sqlite", "postgres"]

    @asynccontextmanager
    async def transaction(self, *, write_lock: bool = False) -> AsyncIterator[AsyncConnection]: ...

    async def close(self) -> None: ...
