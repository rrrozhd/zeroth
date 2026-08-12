"""Materialized current state for external retention cleanup execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncConnection,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

if TYPE_CHECKING:
    from zeroth.governance.retention.cleanup_manifest import CleanupManifest, CleanupOperation


@dataclass(frozen=True, slots=True)
class CleanupStateRecord:
    """Represent CleanupStateRecord within the structural tenant-isolation boundary."""
    authorization_log_id: str
    tenant_id: str
    run_id: str
    reason: str
    generation: int
    revision: int
    active_claim_id: str | None
    active_claim_log_id: str | None
    lease_expires_at: datetime | None
    terminal_status: str | None
    terminal_log_id: str | None


@dataclass(frozen=True, slots=True)
class CleanupOperationRecord:
    """Represent CleanupOperationRecord within the structural tenant-isolation boundary."""
    operation_id: str
    status: str
    deleted_count: int | None
    error: str | None
    revision: int


@persistence_surface(
    "service.retention_cleanup_state",
    probe=named_isolation_probe("_drive_cleanup_state"),
    method_names=frozenset(
        {
            "initialize_in_transaction",
            "get_state_in_transaction",
            "claim_in_transaction",
            "heartbeat_in_transaction",
            "release_in_transaction",
            "terminal_in_transaction",
            "repair_terminal_in_transaction",
        }
    ),
)
@persistence_surface(
    "service.retention_cleanup_operations",
    probe=named_isolation_probe("_drive_cleanup_operations"),
    method_names=frozenset(
        {
            "initialize_in_transaction",
            "get_operation_in_transaction",
            "list_operations_in_transaction",
            "update_operation_in_transaction",
        }
    ),
)
class CleanupStateRepository:
    """Reads and CAS-updates the current cleanup state inside caller transactions."""

    def __init__(self, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext) -> None:
        """Bind the repository or gateway to its validated scope."""
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._scope_context = scope_context
        self._states = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.retention_cleanup_state",
            scope_context,
        )
        self._operations = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.retention_cleanup_operations",
            scope_context,
        )

    @persistence_operation(ResourceOperation.CREATE)
    async def initialize_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        manifest: CleanupManifest,
        generation: int = 0,
        revision: int = 0,
        active_claim_id: str | None = None,
        active_claim_log_id: str | None = None,
        lease_expires_at: datetime | None = None,
        terminal_status: str | None = None,
        terminal_log_id: str | None = None,
    ) -> None:
        """Perform initialize within the caller-owned transaction."""
        if manifest.tenant_id != self._scope_context.tenant_id:
            raise ValueError("cleanup manifest tenant does not match bound scope")
        if any(operation.tenant_id != manifest.tenant_id for operation in manifest.operations):
            raise ValueError("cleanup operation tenant does not match manifest")
        now = datetime.now(UTC).isoformat()
        states = self._states.in_transaction(connection)
        operations = states.bind(self._operations)
        await states.insert(
            {
                "authorization_log_id": authorization_log_id,
                "run_id": manifest.run_id,
                "reason": manifest.reason,
                "generation": generation,
                "revision": revision,
                "active_claim_id": active_claim_id,
                "active_claim_log_id": active_claim_log_id,
                "lease_expires_at": (
                    lease_expires_at.isoformat() if lease_expires_at is not None else None
                ),
                "terminal_status": terminal_status,
                "terminal_log_id": terminal_log_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        for operation in manifest.operations:
            await operations.insert(
                {
                    "authorization_log_id": authorization_log_id,
                    "operation_id": operation.operation_id,
                    "status": operation.status,
                    "deleted_count": operation.deleted_count,
                    "error": operation.error,
                    "revision": revision,
                    "updated_at": now,
                }
            )

    @persistence_operation(ResourceOperation.READ)
    async def get_state_in_transaction(
        self,
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> CleanupStateRecord | None:
        """Perform get state within the caller-owned transaction."""
        row = await self._states.in_transaction(connection).select_one(
            where={"authorization_log_id": authorization_log_id}
        )
        if row is None:
            return None
        lease = row["lease_expires_at"]
        return CleanupStateRecord(
            authorization_log_id=str(row["authorization_log_id"]),
            tenant_id=str(row["tenant_id"]),
            run_id=str(row["run_id"]),
            reason=str(row["reason"]),
            generation=int(row["generation"]),
            revision=int(row["revision"]),
            active_claim_id=row["active_claim_id"],
            active_claim_log_id=row["active_claim_log_id"],
            lease_expires_at=None if lease is None else datetime.fromisoformat(str(lease)),
            terminal_status=row["terminal_status"],
            terminal_log_id=row["terminal_log_id"],
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_operations_in_transaction(
        self,
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> list[CleanupOperationRecord]:
        """Perform list operations within the caller-owned transaction."""
        rows = await self._operations.in_transaction(connection).select(
            where={"authorization_log_id": authorization_log_id},
            columns=("operation_id", "status", "deleted_count", "error", "revision"),
        )
        return [
            CleanupOperationRecord(
                operation_id=str(row["operation_id"]),
                status=str(row["status"]),
                deleted_count=(None if row["deleted_count"] is None else int(row["deleted_count"])),
                error=row["error"],
                revision=int(row["revision"]),
            )
            for row in rows
        ]

    @persistence_operation(ResourceOperation.READ)
    async def get_operation_in_transaction(
        self,
        connection: AsyncConnection,
        authorization_log_id: str,
        operation_id: str,
    ) -> CleanupOperationRecord | None:
        """Perform get operation within the caller-owned transaction."""
        row = await self._operations.in_transaction(connection).select_one(
            where={
                "authorization_log_id": authorization_log_id,
                "operation_id": operation_id,
            },
            columns=("operation_id", "status", "deleted_count", "error", "revision"),
        )
        if row is None:
            return None
        return CleanupOperationRecord(
            operation_id=str(row["operation_id"]),
            status=str(row["status"]),
            deleted_count=(None if row["deleted_count"] is None else int(row["deleted_count"])),
            error=row["error"],
            revision=int(row["revision"]),
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def claim_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        expected_generation: int,
        expected_revision: int,
        claim_id: str,
        claim_log_id: str,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        """Perform claim within the caller-owned transaction."""
        current = await self._require_state(connection, authorization_log_id)
        self._require_revision(current, expected_generation, expected_revision)
        await self._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
            values={
                "generation": expected_generation + 1,
                "revision": expected_revision + 1,
                "active_claim_id": claim_id,
                "active_claim_log_id": claim_log_id,
                "lease_expires_at": lease_expires_at.isoformat(),
                "terminal_status": None,
                "terminal_log_id": None,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        updated = await self._require_state(connection, authorization_log_id)
        if (
            updated.generation != expected_generation + 1
            or updated.revision != expected_revision + 1
            or updated.active_claim_id != claim_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def heartbeat_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        """Perform heartbeat within the caller-owned transaction."""
        current = await self._require_state(connection, authorization_log_id)
        self._require_revision(current, generation, expected_revision, claim_id=claim_id)
        await self._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            where_extra={"active_claim_id": claim_id},
            values={
                "revision": expected_revision + 1,
                "lease_expires_at": lease_expires_at.isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        updated = await self._require_state(connection, authorization_log_id)
        if updated.revision != expected_revision + 1 or updated.active_claim_id != claim_id:
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def update_operation_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        operation: CleanupOperation,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        """Perform update operation within the caller-owned transaction."""
        existing = await self.get_operation_in_transaction(
            connection,
            authorization_log_id,
            operation.operation_id,
        )
        if existing is None:
            raise ValueError("cleanup delta references unknown operation")
        state = await self.heartbeat_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            lease_expires_at=lease_expires_at,
        )
        await self._operations.in_transaction(connection).update(
            {
                "status": operation.status,
                "deleted_count": operation.deleted_count,
                "error": operation.error,
                "revision": state.revision,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            where={
                "authorization_log_id": authorization_log_id,
                "operation_id": operation.operation_id,
            },
        )
        return state

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def release_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
    ) -> CleanupStateRecord:
        """Perform release within the caller-owned transaction."""
        return await self._finish_claim_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            terminal_status=None,
            terminal_log_id=None,
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def terminal_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        terminal_status: str,
        terminal_log_id: str,
    ) -> CleanupStateRecord:
        """Perform terminal within the caller-owned transaction."""
        return await self._finish_claim_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            terminal_status=terminal_status,
            terminal_log_id=terminal_log_id,
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def repair_terminal_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        generation: int,
        expected_revision: int,
        terminal_log_id: str,
    ) -> CleanupStateRecord:
        """Perform repair terminal within the caller-owned transaction."""
        current = await self._require_state(connection, authorization_log_id)
        self._require_revision(current, generation, expected_revision)
        if current.active_claim_id is not None:
            raise RuntimeError("cleanup state compare-and-swap failed")
        await self._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            where_extra={"active_claim_id": None},
            values={
                "revision": expected_revision + 1,
                "terminal_status": "completed",
                "terminal_log_id": terminal_log_id,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        updated = await self._require_state(connection, authorization_log_id)
        if (
            updated.revision != expected_revision + 1
            or updated.active_claim_id is not None
            or updated.terminal_status != "completed"
            or updated.terminal_log_id != terminal_log_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    async def _finish_claim_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        terminal_status: str | None,
        terminal_log_id: str | None,
    ) -> CleanupStateRecord:
        """Resolve finish claim in transaction for structurally scoped persistence."""
        current = await self._require_state(connection, authorization_log_id)
        self._require_revision(current, generation, expected_revision, claim_id=claim_id)
        await self._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            where_extra={"active_claim_id": claim_id},
            values={
                "revision": expected_revision + 1,
                "active_claim_id": None,
                "active_claim_log_id": None,
                "lease_expires_at": None,
                "terminal_status": terminal_status,
                "terminal_log_id": terminal_log_id,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        updated = await self._require_state(connection, authorization_log_id)
        if (
            updated.revision != expected_revision + 1
            or updated.active_claim_id is not None
            or updated.terminal_status != terminal_status
            or updated.terminal_log_id != terminal_log_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    async def _update_state_cas(
        self,
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        expected_generation: int,
        expected_revision: int,
        values: dict[str, object],
        where_extra: dict[str, object] | None = None,
    ) -> None:
        """Resolve update state cas for structurally scoped persistence."""
        matched = await self._states.in_transaction(connection).update_if_matches(
            values,
            where={
                "authorization_log_id": authorization_log_id,
                "generation": expected_generation,
                "revision": expected_revision,
                **(where_extra or {}),
            },
            returning="revision",
        )
        if not matched:
            raise RuntimeError("cleanup state compare-and-swap failed")

    @staticmethod
    def _require_revision(
        state: CleanupStateRecord,
        generation: int,
        revision: int,
        *,
        claim_id: str | None = None,
    ) -> None:
        """Validate revision before accessing scoped persistence."""
        if state.generation != generation or state.revision != revision:
            raise RuntimeError("cleanup state compare-and-swap failed")
        if claim_id is not None and state.active_claim_id != claim_id:
            raise RuntimeError("cleanup state compare-and-swap failed")

    async def _require_state(
        self,
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> CleanupStateRecord:
        """Validate state before accessing scoped persistence."""
        state = await self.get_state_in_transaction(connection, authorization_log_id)
        if state is None:
            raise ValueError("cleanup authorization state disappeared")
        return state
