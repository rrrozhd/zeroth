"""Backend-conditional async lease manager for durable run dispatch.

Supports two claiming strategies:
- **Postgres**: ``SELECT ... FOR UPDATE SKIP LOCKED`` for contention-free
  multi-worker claiming.  No verify step needed.
- **SQLite**: Timestamp-expiry UPDATE with a verify re-read (the existing
  approach).  Works for single-node deployments.

Each pending run is claimed by a worker via an atomic operation that sets
lease columns.  If a worker crashes, its lease expires and another worker
can reclaim the run.  The lease is renewed periodically while the run is
executing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from zeroth.platform.storage import AsyncDatabase

try:
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    _HAS_PG = True
except ImportError:
    _HAS_PG = False

# The status-column vocabulary of the runs table this SQL claims from. These
# are the persisted string values of the run domain's RunStatus enum; the
# platform layer sits below the run domain, so it speaks the column contract
# rather than importing the enum. tests/dispatch/test_lease.py pins the
# values against RunStatus.
_STATUS_PENDING = "PENDING"
_STATUS_RUNNING = "RUNNING"

# The columns that constitute the fence itself. A fenced write may never
# touch them: doing so would let a displaced worker re-grant its own lease.
_FENCE_COLUMNS = frozenset(
    {"lease_worker_id", "lease_generation", "lease_acquired_at", "lease_expires_at"}
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_worker_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class LeaseManager:
    """Manages worker leases on runs stored in an async database.

    A lease is an exclusive claim on a run.  Workers use leases to prevent
    two concurrent workers from both executing the same run.  Leases expire
    after ``lease_duration_seconds`` so a crashed worker's work can be reclaimed.
    """

    database: AsyncDatabase
    lease_duration_seconds: int = 60

    # ---------------------------------------------------------------------------
    # Backend detection
    # ---------------------------------------------------------------------------

    def _is_postgres(self) -> bool:
        """Detect Postgres backend for SKIP LOCKED support."""
        return _HAS_PG and isinstance(self.database, AsyncPostgresDatabase)

    # ---------------------------------------------------------------------------
    # Claim operations
    # ---------------------------------------------------------------------------

    async def claim_pending(self, deployment_ref: str, worker_id: str) -> str | None:
        """Atomically claim one PENDING run for this worker.

        Dispatches to ``_claim_pending_pg`` (Postgres) or
        ``_claim_pending_sqlite`` (SQLite) based on the database backend.

        Returns the run_id that was claimed, or None if no work is available.
        The claimed run's status is left as PENDING -- the worker transitions
        it to RUNNING once execution actually starts.
        """
        if self._is_postgres():
            return await self._claim_pending_pg(deployment_ref, worker_id)
        return await self._claim_pending_sqlite(deployment_ref, worker_id)

    async def _claim_pending_sqlite(self, deployment_ref: str, worker_id: str) -> str | None:
        """Claim using timestamp-expiry UPDATE with verify re-read (SQLite)."""
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.lease_duration_seconds)
        async with self.database.transaction() as conn:
            # Pick the oldest PENDING unleased run for this deployment.
            row = await conn.fetch_one(
                """
                SELECT run_id FROM runs
                WHERE deployment_ref = ?
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                ORDER BY started_at ASC
                LIMIT 1
                """,
                (deployment_ref, _STATUS_PENDING, now.isoformat()),
            )
            if row is None:
                return None
            run_id = row["run_id"]
            # Atomic write-lock: only one concurrent writer can update this row.
            # The generation advances with the claim so a displaced worker's
            # writes can be told apart from the new owner's.
            await conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?,
                    lease_generation = lease_generation + 1
                WHERE run_id = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                """,
                (
                    worker_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                    run_id,
                    now.isoformat(),
                ),
            )
            # Verify we actually won the race (rowcount == 1).
            verify_row = await conn.fetch_one(
                "SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,)
            )
            if verify_row is None or verify_row["lease_worker_id"] != worker_id:
                return None
        return run_id

    async def _claim_pending_pg(self, deployment_ref: str, worker_id: str) -> str | None:
        """Atomic claim using SELECT ... FOR UPDATE SKIP LOCKED (Postgres).

        Workers skip rows already being claimed by another worker.
        No verify step needed -- the lock is acquired at SELECT time.
        """
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.lease_duration_seconds)
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                """
                SELECT run_id FROM runs
                WHERE deployment_ref = ?
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                ORDER BY started_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (deployment_ref, _STATUS_PENDING, now.isoformat()),
            )
            if row is None:
                return None
            run_id = row["run_id"]
            await conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?,
                    lease_generation = lease_generation + 1
                WHERE run_id = ?
                """,
                (worker_id, now.isoformat(), expires_at.isoformat(), run_id),
            )
        return run_id

    async def claim_orphaned(self, deployment_ref: str, worker_id: str) -> list[str]:
        """Claim all RUNNING runs with expired leases for this deployment.

        Called at worker startup to recover work abandoned by crashed workers.
        Sets ``recovery_checkpoint_id`` to the latest checkpoint for each
        claimed run so the worker knows where to resume.
        """
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.lease_duration_seconds)
        claimed: list[str] = []
        async with self.database.transaction() as conn:
            rows = await conn.fetch_all(
                """
                SELECT run_id FROM runs
                WHERE deployment_ref = ?
                  AND status = ?
                  AND lease_worker_id IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (deployment_ref, _STATUS_RUNNING, now.isoformat()),
            )
            for row in rows:
                run_id = row["run_id"]
                # Find the latest checkpoint for this run.
                cp_row = await conn.fetch_one(
                    """
                    SELECT checkpoint_id FROM run_checkpoints
                    WHERE run_id = ?
                    ORDER BY checkpoint_order DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
                recovery_checkpoint_id = cp_row["checkpoint_id"] if cp_row else None
                await conn.execute(
                    """
                    UPDATE runs
                    SET lease_worker_id = ?,
                        lease_acquired_at = ?,
                        lease_expires_at = ?,
                        recovery_checkpoint_id = ?,
                        lease_generation = lease_generation + 1
                    WHERE run_id = ?
                    """,
                    (
                        worker_id,
                        now.isoformat(),
                        expires_at.isoformat(),
                        recovery_checkpoint_id,
                        run_id,
                    ),
                )
                claimed.append(run_id)
        return claimed

    # ---------------------------------------------------------------------------
    # Lease maintenance
    # ---------------------------------------------------------------------------

    async def renew_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Extend the lease expiry for an active run.

        Returns True if the lease was renewed (i.e. we still own it), False if
        another worker has taken over or the run no longer exists.

        ``generation`` qualifies the renewal on top of ownership.  Worker ids are
        fresh per process, so owner-qualification alone already catches takeover
        by a *different* worker; the generation additionally catches the case
        where the lease was released and re-acquired, and is what the caller
        must then present to :meth:`commit_fenced`.
        """
        now = _utc_now()
        new_expires = now + timedelta(seconds=self.lease_duration_seconds)
        async with self.database.transaction() as conn:
            if generation is None:
                await conn.execute(
                    """
                    UPDATE runs
                    SET lease_expires_at = ?
                    WHERE run_id = ? AND lease_worker_id = ?
                    """,
                    (new_expires.isoformat(), run_id, worker_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE runs
                    SET lease_expires_at = ?
                    WHERE run_id = ?
                      AND lease_worker_id = ?
                      AND lease_generation = ?
                    """,
                    (new_expires.isoformat(), run_id, worker_id, generation),
                )
            row = await conn.fetch_one(
                "SELECT lease_worker_id, lease_generation FROM runs WHERE run_id = ?",
                (run_id,),
            )
        if row is None:
            return False
        if row["lease_worker_id"] != worker_id:
            return False
        return generation is None or int(row["lease_generation"]) == generation

    async def current_generation(self, run_id: str) -> int | None:
        """The run's current lease generation, or None if the run is unknown."""
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT lease_generation FROM runs WHERE run_id = ?", (run_id,)
            )
        return None if row is None else int(row["lease_generation"])

    async def commit_fenced(
        self,
        run_id: str,
        worker_id: str,
        *,
        generation: int,
        metrics_collector: object | None = None,
        **columns: object,
    ) -> bool:
        """Apply a run-state write only if the caller still holds the lease.

        The fence is part of the UPDATE predicate rather than a preceding check,
        because a check-then-write leaves a window in which ownership can move
        between the two statements -- precisely the race this exists to close.

        Returns True when the write landed, False when a newer generation (or a
        different owner) has superseded the caller.
        """
        if not columns:
            raise ValueError("commit_fenced requires at least one column to write")
        forbidden = _FENCE_COLUMNS.intersection(columns)
        if forbidden:
            # A caller that could write the fence columns could grant itself the
            # lease it is being fenced against, which makes the fence decorative.
            raise ValueError(
                f"commit_fenced may not write lease columns: {sorted(forbidden)}"
            )
        assignments = ", ".join(f"{name} = ?" for name in columns)
        params = (
            *columns.values(),
            run_id,
            worker_id,
            generation,
        )
        async with self.database.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE runs
                SET {assignments}
                WHERE run_id = ?
                  AND lease_worker_id = ?
                  AND lease_generation = ?
                """,
                params,
            )
            row = await conn.fetch_one(
                "SELECT lease_worker_id, lease_generation FROM runs WHERE run_id = ?",
                (run_id,),
            )
        if row is None:
            applied = False
        else:
            applied = (
                row["lease_worker_id"] == worker_id and int(row["lease_generation"]) == generation
            )
        if not applied and metrics_collector is not None:
            metrics_collector.increment("zeroth_lease_fencing_rejected_total")
        return applied

    async def release_lease(self, run_id: str, worker_id: str) -> None:
        """Clear the lease columns after a run finishes (success or failure)."""
        async with self.database.transaction() as conn:
            await conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = NULL,
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    recovery_checkpoint_id = NULL
                WHERE run_id = ? AND lease_worker_id = ?
                """,
                (run_id, worker_id),
            )

    async def clear_lease(self, run_id: str) -> None:
        """Clear the lease columns regardless of the current lease owner."""
        async with self.database.transaction() as conn:
            await conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = NULL,
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    recovery_checkpoint_id = NULL
                WHERE run_id = ?
                """,
                (run_id,),
            )

    async def get_recovery_checkpoint_id(self, run_id: str) -> str | None:
        """Return the recovery_checkpoint_id stored on the run, if any."""
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT recovery_checkpoint_id FROM runs WHERE run_id = ?",
                (run_id,),
            )
        return row["recovery_checkpoint_id"] if row else None
