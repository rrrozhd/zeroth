"""Materialized current state for external retention cleanup execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zeroth.core.retention.cleanup_manifest import CleanupManifest, CleanupOperation
    from zeroth.platform.storage import AsyncConnection


@dataclass(frozen=True, slots=True)
class CleanupStateRecord:
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
    operation_id: str
    status: str
    deleted_count: int | None
    error: str | None
    revision: int


class CleanupStateRepository:
    """Reads and CAS-updates the current cleanup state inside caller transactions."""

    @staticmethod
    async def initialize_in_transaction(
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
        now = datetime.now(UTC).isoformat()
        await connection.execute(
            """
            INSERT INTO retention_cleanup_state
                (authorization_log_id, tenant_id, run_id, reason, generation, revision,
                 active_claim_id, active_claim_log_id, lease_expires_at,
                 terminal_status, terminal_log_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                authorization_log_id,
                manifest.tenant_id,
                manifest.run_id,
                manifest.reason,
                generation,
                revision,
                active_claim_id,
                active_claim_log_id,
                lease_expires_at.isoformat() if lease_expires_at is not None else None,
                terminal_status,
                terminal_log_id,
                now,
                now,
            ),
        )
        for operation in manifest.operations:
            await connection.execute(
                """
                INSERT INTO retention_cleanup_operations
                    (authorization_log_id, operation_id, status, deleted_count,
                     error, revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization_log_id,
                    operation.operation_id,
                    operation.status,
                    operation.deleted_count,
                    operation.error,
                    revision,
                    now,
                ),
            )

    @staticmethod
    async def get_state_in_transaction(
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> CleanupStateRecord | None:
        row = await connection.fetch_one(
            "SELECT * FROM retention_cleanup_state WHERE authorization_log_id = ?",
            (authorization_log_id,),
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

    @staticmethod
    async def list_operations_in_transaction(
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> list[CleanupOperationRecord]:
        rows = await connection.fetch_all(
            """
            SELECT operation_id, status, deleted_count, error, revision
            FROM retention_cleanup_operations
            WHERE authorization_log_id = ?
            """,
            (authorization_log_id,),
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

    @staticmethod
    async def get_operation_in_transaction(
        connection: AsyncConnection,
        authorization_log_id: str,
        operation_id: str,
    ) -> CleanupOperationRecord | None:
        row = await connection.fetch_one(
            """
            SELECT operation_id, status, deleted_count, error, revision
            FROM retention_cleanup_operations
            WHERE authorization_log_id = ? AND operation_id = ?
            """,
            (authorization_log_id, operation_id),
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

    @staticmethod
    async def claim_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        expected_generation: int,
        expected_revision: int,
        claim_id: str,
        claim_log_id: str,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        current = await CleanupStateRepository._require_state(connection, authorization_log_id)
        CleanupStateRepository._require_revision(current, expected_generation, expected_revision)
        await CleanupStateRepository._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
            sql="""
                UPDATE retention_cleanup_state
                SET generation = ?, revision = ?, active_claim_id = ?,
                    active_claim_log_id = ?, lease_expires_at = ?,
                    terminal_status = NULL, terminal_log_id = NULL, updated_at = ?
                WHERE authorization_log_id = ? AND generation = ? AND revision = ?
            """,
            params=(
                expected_generation + 1,
                expected_revision + 1,
                claim_id,
                claim_log_id,
                lease_expires_at.isoformat(),
                datetime.now(UTC).isoformat(),
                authorization_log_id,
                expected_generation,
                expected_revision,
            ),
        )
        updated = await CleanupStateRepository._require_state(connection, authorization_log_id)
        if (
            updated.generation != expected_generation + 1
            or updated.revision != expected_revision + 1
            or updated.active_claim_id != claim_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @staticmethod
    async def heartbeat_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        current = await CleanupStateRepository._require_state(connection, authorization_log_id)
        CleanupStateRepository._require_revision(
            current, generation, expected_revision, claim_id=claim_id
        )
        await CleanupStateRepository._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            sql="""
                UPDATE retention_cleanup_state
                SET revision = ?, lease_expires_at = ?, updated_at = ?
                WHERE authorization_log_id = ? AND active_claim_id = ?
                    AND generation = ? AND revision = ?
            """,
            params=(
                expected_revision + 1,
                lease_expires_at.isoformat(),
                datetime.now(UTC).isoformat(),
                authorization_log_id,
                claim_id,
                generation,
                expected_revision,
            ),
        )
        updated = await CleanupStateRepository._require_state(connection, authorization_log_id)
        if updated.revision != expected_revision + 1 or updated.active_claim_id != claim_id:
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @staticmethod
    async def update_operation_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        operation: CleanupOperation,
        lease_expires_at: datetime,
    ) -> CleanupStateRecord:
        existing = await CleanupStateRepository.get_operation_in_transaction(
            connection,
            authorization_log_id,
            operation.operation_id,
        )
        if existing is None:
            raise ValueError("cleanup delta references unknown operation")
        state = await CleanupStateRepository.heartbeat_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            lease_expires_at=lease_expires_at,
        )
        await connection.execute(
            """
            UPDATE retention_cleanup_operations
            SET status = ?, deleted_count = ?, error = ?, revision = ?, updated_at = ?
            WHERE authorization_log_id = ? AND operation_id = ?
            """,
            (
                operation.status,
                operation.deleted_count,
                operation.error,
                state.revision,
                datetime.now(UTC).isoformat(),
                authorization_log_id,
                operation.operation_id,
            ),
        )
        return state

    @staticmethod
    async def release_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
    ) -> CleanupStateRecord:
        return await CleanupStateRepository._finish_claim_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            terminal_status=None,
            terminal_log_id=None,
        )

    @staticmethod
    async def terminal_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        terminal_status: str,
        terminal_log_id: str,
    ) -> CleanupStateRecord:
        return await CleanupStateRepository._finish_claim_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            expected_revision=expected_revision,
            terminal_status=terminal_status,
            terminal_log_id=terminal_log_id,
        )

    @staticmethod
    async def repair_terminal_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        generation: int,
        expected_revision: int,
        terminal_log_id: str,
    ) -> CleanupStateRecord:
        current = await CleanupStateRepository._require_state(connection, authorization_log_id)
        CleanupStateRepository._require_revision(current, generation, expected_revision)
        if current.active_claim_id is not None:
            raise RuntimeError("cleanup state compare-and-swap failed")
        await CleanupStateRepository._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            sql="""
                UPDATE retention_cleanup_state
                SET revision = ?, terminal_status = 'completed', terminal_log_id = ?,
                    updated_at = ?
                WHERE authorization_log_id = ? AND generation = ? AND revision = ?
                    AND active_claim_id IS NULL
            """,
            params=(
                expected_revision + 1,
                terminal_log_id,
                datetime.now(UTC).isoformat(),
                authorization_log_id,
                generation,
                expected_revision,
            ),
        )
        updated = await CleanupStateRepository._require_state(connection, authorization_log_id)
        if (
            updated.revision != expected_revision + 1
            or updated.active_claim_id is not None
            or updated.terminal_status != "completed"
            or updated.terminal_log_id != terminal_log_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @staticmethod
    async def _finish_claim_in_transaction(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        expected_revision: int,
        terminal_status: str | None,
        terminal_log_id: str | None,
    ) -> CleanupStateRecord:
        current = await CleanupStateRepository._require_state(connection, authorization_log_id)
        CleanupStateRepository._require_revision(
            current, generation, expected_revision, claim_id=claim_id
        )
        await CleanupStateRepository._update_state_cas(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=generation,
            expected_revision=expected_revision,
            sql="""
                UPDATE retention_cleanup_state
                SET revision = ?, active_claim_id = NULL, active_claim_log_id = NULL,
                    lease_expires_at = NULL, terminal_status = ?, terminal_log_id = ?,
                    updated_at = ?
                WHERE authorization_log_id = ? AND active_claim_id = ?
                    AND generation = ? AND revision = ?
            """,
            params=(
                expected_revision + 1,
                terminal_status,
                terminal_log_id,
                datetime.now(UTC).isoformat(),
                authorization_log_id,
                claim_id,
                generation,
                expected_revision,
            ),
        )
        updated = await CleanupStateRepository._require_state(connection, authorization_log_id)
        if (
            updated.revision != expected_revision + 1
            or updated.active_claim_id is not None
            or updated.terminal_status != terminal_status
            or updated.terminal_log_id != terminal_log_id
        ):
            raise RuntimeError("cleanup state compare-and-swap failed")
        return updated

    @staticmethod
    async def _update_state_cas(
        connection: AsyncConnection,
        *,
        authorization_log_id: str,
        expected_generation: int,
        expected_revision: int,
        sql: str,
        params: tuple[Any, ...],
    ) -> None:
        await connection.execute(sql, params)
        state = await CleanupStateRepository._require_state(connection, authorization_log_id)
        if state.generation == expected_generation and state.revision == expected_revision:
            raise RuntimeError("cleanup state compare-and-swap failed")

    @staticmethod
    def _require_revision(
        state: CleanupStateRecord,
        generation: int,
        revision: int,
        *,
        claim_id: str | None = None,
    ) -> None:
        if state.generation != generation or state.revision != revision:
            raise RuntimeError("cleanup state compare-and-swap failed")
        if claim_id is not None and state.active_claim_id != claim_id:
            raise RuntimeError("cleanup state compare-and-swap failed")

    @staticmethod
    async def _require_state(
        connection: AsyncConnection,
        authorization_log_id: str,
    ) -> CleanupStateRecord:
        state = await CleanupStateRepository.get_state_in_transaction(
            connection, authorization_log_id
        )
        if state is None:
            raise ValueError("cleanup authorization state disappeared")
        return state
