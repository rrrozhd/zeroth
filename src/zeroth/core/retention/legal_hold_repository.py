"""Async storage for legal holds (WS-E).

A legal hold freezes data against deletion. It beats BOTH TTL purge and explicit
right-to-erasure: while a hold is active the erasure service refuses to touch the
covered run(s). ``run_id is None`` places a tenant-wide hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from zeroth.core.retention.coordination import RetentionCoordinator
from zeroth.core.retention.models import LegalHold, TenantHolds
from zeroth.core.storage import AsyncConnection, AsyncDatabase


class LegalHoldRepository:
    """Place, release, and query legal holds over ``legal_holds``."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database
        self._coordinator = RetentionCoordinator(database)

    async def place(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        placed_by: str | None = None,
    ) -> LegalHold:
        """Place a hold (run-scoped when ``run_id`` given, else tenant-wide)."""
        async with self._coordinator.transaction(tenant_id) as connection:
            return await self.place_in_transaction(
                connection,
                tenant_id,
                run_id=run_id,
                reason=reason,
                placed_by=placed_by,
            )

    async def place_in_transaction(
        self,
        connection: AsyncConnection,
        tenant_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        placed_by: str | None = None,
    ) -> LegalHold:
        """Place a hold using an existing tenant coordination transaction."""
        hold = LegalHold(
            hold_id=uuid4().hex,
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
            active=True,
            placed_by=placed_by,
            created_at=datetime.now(UTC),
        )
        await connection.execute(
            """
            INSERT INTO legal_holds
                (hold_id, tenant_id, run_id, reason, active, placed_by,
                 created_at, released_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, NULL)
            """,
            (
                hold.hold_id,
                hold.tenant_id,
                hold.run_id,
                hold.reason,
                hold.placed_by,
                hold.created_at.isoformat(),
            ),
        )
        return hold

    async def release(self, hold_id: str) -> bool:
        """Release (deactivate) a hold. Idempotent; returns True if it existed."""
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                "SELECT tenant_id FROM legal_holds WHERE hold_id = ?",
                (hold_id,),
            )
        if existing is None:
            return False
        async with self._coordinator.transaction(str(existing["tenant_id"])) as connection:
            return await self.release_in_transaction(connection, hold_id)

    async def release_in_transaction(
        self,
        connection: AsyncConnection,
        hold_id: str,
    ) -> bool:
        """Release a hold using an existing tenant coordination transaction."""
        existing = await connection.fetch_one(
            "SELECT 1 FROM legal_holds WHERE hold_id = ?",
            (hold_id,),
        )
        if existing is None:
            return False
        await connection.execute(
            "UPDATE legal_holds SET active = 0, released_at = ? WHERE hold_id = ?",
            (datetime.now(UTC).isoformat(), hold_id),
        )
        return True

    async def get(self, hold_id: str) -> LegalHold | None:
        """Load a single hold by id."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT * FROM legal_holds WHERE hold_id = ?",
                (hold_id,),
            )
        return None if row is None else self._row_to_hold(row)

    async def list_for_tenant(self, tenant_id: str, *, active_only: bool = True) -> list[LegalHold]:
        """List holds for a tenant, active-only by default."""
        sql = "SELECT * FROM legal_holds WHERE tenant_id = ?"
        params: tuple[object, ...] = (tenant_id,)
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at, hold_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        return [self._row_to_hold(row) for row in rows]

    async def active_holds_for_tenant(self, tenant_id: str) -> TenantHolds:
        """Resolve active holds into a tenant-wide flag + held run_id set."""
        async with self._coordinator.transaction(tenant_id) as connection:
            return await self.active_holds_for_tenant_in_transaction(connection, tenant_id)

    async def active_holds_for_tenant_in_transaction(
        self,
        connection: AsyncConnection,
        tenant_id: str,
    ) -> TenantHolds:
        """Resolve active holds using an existing tenant coordination transaction."""
        rows = await connection.fetch_all(
            """
            SELECT * FROM legal_holds
            WHERE tenant_id = ? AND active = 1
            ORDER BY created_at, hold_id
            """,
            (tenant_id,),
        )
        holds = [self._row_to_hold(row) for row in rows]
        tenant_wide = any(hold.run_id is None for hold in holds)
        run_ids = {hold.run_id for hold in holds if hold.run_id is not None}
        return TenantHolds(tenant_wide=tenant_wide, run_ids=run_ids)

    def _row_to_hold(self, row: dict[str, object]) -> LegalHold:
        released_at = row["released_at"]
        return LegalHold(
            hold_id=str(row["hold_id"]),
            tenant_id=str(row["tenant_id"]),
            run_id=row["run_id"],  # type: ignore[arg-type]
            reason=row["reason"],  # type: ignore[arg-type]
            active=bool(row["active"]),
            placed_by=row["placed_by"],  # type: ignore[arg-type]
            created_at=datetime.fromisoformat(str(row["created_at"])),
            released_at=(datetime.fromisoformat(str(released_at)) if released_at else None),
        )
