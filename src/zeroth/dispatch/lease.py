"""Backend-conditional lease manager for durable run dispatch.

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

from zeroth.runs import RunStatus
from zeroth.storage import SQLiteDatabase

try:
    from zeroth.storage.async_postgres import AsyncPostgresDatabase

    _HAS_PG = True
except ImportError:
    _HAS_PG = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_worker_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class LeaseManager:
    """Manages worker leases on runs stored in SQLite or Postgres.

    A lease is an exclusive claim on a run.  Workers use leases to prevent
    two concurrent workers from both executing the same run.  Leases expire
    after ``lease_duration_seconds`` so a crashed worker's work can be reclaimed.
    """

    database: SQLiteDatabase  # or AsyncPostgresDatabase at runtime
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

    def claim_pending(self, deployment_ref: str, worker_id: str) -> str | None:
        """Atomically claim one PENDING run for this worker.

        Dispatches to ``_claim_pending_pg`` (Postgres) or
        ``_claim_pending_sqlite`` (SQLite) based on the database backend.

        Returns the run_id that was claimed, or None if no work is available.
        The claimed run's status is left as PENDING -- the worker transitions
        it to RUNNING once execution actually starts.
        """
        if self._is_postgres():
            return self._claim_pending_pg(deployment_ref, worker_id)
        return self._claim_pending_sqlite(deployment_ref, worker_id)

    def _claim_pending_sqlite(self, deployment_ref: str, worker_id: str) -> str | None:
        """Claim using timestamp-expiry UPDATE with verify re-read (SQLite)."""
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.lease_duration_seconds)
        with self.database.transaction() as conn:
            # Pick the oldest PENDING unleased run for this deployment.
            row = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE deployment_ref = ?
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                ORDER BY started_at ASC
                LIMIT 1
                """,
                (deployment_ref, RunStatus.PENDING.value, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            run_id = row["run_id"]
            # Atomic write-lock: only one concurrent writer can update this row.
            conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?
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
            if conn.execute(
                "SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["lease_worker_id"] != worker_id:
                return None
        return run_id

    def _claim_pending_pg(self, deployment_ref: str, worker_id: str) -> str | None:
        """Atomic claim using SELECT ... FOR UPDATE SKIP LOCKED (Postgres).

        Workers skip rows already being claimed by another worker.
        No verify step needed -- the lock is acquired at SELECT time.

        This method runs synchronously by driving the async Postgres
        transaction through an internal event loop helper.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We're inside an existing event loop (e.g. called from worker poll).
            # Create a future and use the existing loop.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run, self._claim_pending_pg_async(deployment_ref, worker_id)
                ).result()
        return asyncio.run(self._claim_pending_pg_async(deployment_ref, worker_id))

    async def _claim_pending_pg_async(
        self, deployment_ref: str, worker_id: str
    ) -> str | None:
        """Async implementation of Postgres SKIP LOCKED claiming."""
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
                (deployment_ref, RunStatus.PENDING.value, now.isoformat()),
            )
            if row is None:
                return None
            run_id = row["run_id"]
            await conn.execute(
                """
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?
                WHERE run_id = ?
                """,
                (worker_id, now.isoformat(), expires_at.isoformat(), run_id),
            )
        return run_id

    def claim_orphaned(self, deployment_ref: str, worker_id: str) -> list[str]:
        """Claim all RUNNING runs with expired leases for this deployment.

        Called at worker startup to recover work abandoned by crashed workers.
        Sets ``recovery_checkpoint_id`` to the latest checkpoint for each
        claimed run so the worker knows where to resume.
        """
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.lease_duration_seconds)
        claimed: list[str] = []
        with self.database.transaction() as conn:
            rows = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE deployment_ref = ?
                  AND status = ?
                  AND lease_worker_id IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (deployment_ref, RunStatus.RUNNING.value, now.isoformat()),
            ).fetchall()
            for row in rows:
                run_id = row["run_id"]
                # Find the latest checkpoint for this run.
                cp_row = conn.execute(
                    """
                    SELECT checkpoint_id FROM run_checkpoints
                    WHERE run_id = ?
                    ORDER BY checkpoint_order DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                recovery_checkpoint_id = cp_row["checkpoint_id"] if cp_row else None
                conn.execute(
                    """
                    UPDATE runs
                    SET lease_worker_id = ?,
                        lease_acquired_at = ?,
                        lease_expires_at = ?,
                        recovery_checkpoint_id = ?
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

    def renew_lease(self, run_id: str, worker_id: str) -> bool:
        """Extend the lease expiry for an active run.

        Returns True if the lease was renewed (i.e. we still own it), False if
        another worker has taken over or the run no longer exists.
        """
        now = _utc_now()
        new_expires = now + timedelta(seconds=self.lease_duration_seconds)
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                SET lease_expires_at = ?
                WHERE run_id = ? AND lease_worker_id = ?
                """,
                (new_expires.isoformat(), run_id, worker_id),
            )
            row = conn.execute(
                "SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return False
        return row["lease_worker_id"] == worker_id

    def release_lease(self, run_id: str, worker_id: str) -> None:
        """Clear the lease columns after a run finishes (success or failure)."""
        with self.database.transaction() as conn:
            conn.execute(
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

    def clear_lease(self, run_id: str) -> None:
        """Clear the lease columns regardless of the current lease owner."""
        with self.database.transaction() as conn:
            conn.execute(
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

    def get_recovery_checkpoint_id(self, run_id: str) -> str | None:
        """Return the recovery_checkpoint_id stored on the run, if any."""
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT recovery_checkpoint_id FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row["recovery_checkpoint_id"] if row else None
