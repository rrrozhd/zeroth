"""Database-backed coordination for tenant retention administration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from zeroth.core.storage import AsyncConnection, AsyncDatabase, ensure_and_lock_row


class RetentionCoordinator:
    """Serialize retention decisions and legal-hold changes per tenant."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self, tenant_id: str) -> AsyncIterator[AsyncConnection]:
        """Yield a write transaction holding the tenant coordination row."""
        async with self._database.transaction(write_lock=True) as connection:
            row = await ensure_and_lock_row(
                connection,
                backend=self._database.backend,
                table="retention_coordination",
                key_column="tenant_id",
                key=tenant_id,
            )
            if row is None:  # pragma: no cover - INSERT + SELECT is invariant
                raise RuntimeError(f"failed to initialize retention lock for {tenant_id!r}")
            yield connection
