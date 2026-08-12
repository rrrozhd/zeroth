"""Async storage for legal holds (WS-E).

A legal hold freezes data against deletion. It beats BOTH TTL purge and explicit
right-to-erasure: while a hold is active the erasure service refuses to touch the
covered run(s). ``run_id is None`` places a tenant-wide hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from zeroth.governance.retention.coordination import RetentionCoordinator, RetentionTransaction
from zeroth.governance.retention.models import LegalHold, TenantHolds
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoping import ResourceOperation, persistence_operation


class LegalHoldRepository:
    """Place, release, and query legal holds over ``legal_holds``."""

    def __init__(self, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext) -> None:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._database = database
        self._scope_context = scope_context
        self._holds = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.legal_holds", scope_context
        )
        self._coordinator = RetentionCoordinator(database, scope_context)

    @classmethod
    def for_default_compatibility(cls, database: AsyncDatabase) -> LegalHoldRepository:
        return cls(database, NullWorkspaceScopeContext.for_default_compatibility())

    @persistence_operation(ResourceOperation.CREATE)
    async def place(
        self,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        placed_by: str | None = None,
    ) -> LegalHold:
        """Place a hold (run-scoped when ``run_id`` given, else tenant-wide)."""
        async with self._coordinator.transaction() as transaction:
            return await self.place_in_transaction(
                transaction,
                run_id=run_id,
                reason=reason,
                placed_by=placed_by,
            )

    @persistence_operation(ResourceOperation.CREATE)
    async def place_in_transaction(
        self,
        transaction: RetentionTransaction,
        *,
        run_id: str | None = None,
        reason: str | None = None,
        placed_by: str | None = None,
    ) -> LegalHold:
        """Place a hold using an existing tenant coordination transaction."""
        hold = LegalHold(
            hold_id=uuid4().hex,
            tenant_id=transaction.tenant_id,
            run_id=run_id,
            reason=reason,
            active=True,
            placed_by=placed_by,
            created_at=datetime.now(UTC),
        )
        await self._holds.in_transaction(transaction.connection).insert(
            {
                "hold_id": hold.hold_id,
                "run_id": hold.run_id,
                "reason": hold.reason,
                "active": 1,
                "placed_by": hold.placed_by,
                "created_at": hold.created_at.isoformat(),
                "released_at": None,
            }
        )
        return hold

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def release(self, hold_id: str) -> bool:
        """Release (deactivate) a hold. Idempotent; returns True if it existed."""
        existing = await self._holds.select_one(where={"hold_id": hold_id}, columns=("hold_id",))
        if existing is None:
            return False
        async with self._coordinator.transaction() as transaction:
            return await self.release_in_transaction(transaction, hold_id)

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def release_in_transaction(
        self,
        transaction: RetentionTransaction,
        hold_id: str,
    ) -> bool:
        """Release a hold using an existing tenant coordination transaction."""
        holds = self._holds.in_transaction(transaction.connection)
        existing = await holds.select_one(where={"hold_id": hold_id}, columns=("hold_id",))
        if existing is None:
            return False
        await holds.update(
            {"active": 0, "released_at": datetime.now(UTC).isoformat()},
            where={"hold_id": hold_id},
        )
        return True

    @persistence_operation(ResourceOperation.READ)
    async def get(self, hold_id: str) -> LegalHold | None:
        """Load a single hold by id."""
        row = await self._holds.select_one(where={"hold_id": hold_id})
        return None if row is None else self._row_to_hold(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_for_tenant(self, *, active_only: bool = True) -> list[LegalHold]:
        """List holds for a tenant, active-only by default."""
        where = {"active": 1} if active_only else None
        async with self._holds.transaction() as holds:
            rows = await holds.select(where=where, order_by=("created_at", "hold_id"))
        return [self._row_to_hold(row) for row in rows]

    @persistence_operation(ResourceOperation.ENUMERATE, ResourceOperation.READ)
    async def active_holds_for_tenant(self) -> TenantHolds:
        """Resolve active holds into a tenant-wide flag + held run_id set."""
        async with self._coordinator.transaction() as transaction:
            return await self.active_holds_for_tenant_in_transaction(transaction)

    @persistence_operation(ResourceOperation.ENUMERATE, ResourceOperation.READ)
    async def active_holds_for_tenant_in_transaction(
        self,
        transaction: RetentionTransaction,
    ) -> TenantHolds:
        """Resolve active holds using an existing tenant coordination transaction."""
        rows = await self._holds.in_transaction(transaction.connection).select(
            where={"active": 1}, order_by=("created_at", "hold_id")
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
