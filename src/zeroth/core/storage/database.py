"""Abstract async database protocol for all Zeroth repositories.

Defines the AsyncDatabase and AsyncConnection protocols that both
the SQLite and Postgres backends implement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable


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

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]: ...

    async def close(self) -> None: ...
