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
from datetime import datetime, timedelta
from uuid import uuid4

from zeroth.platform.storage import AsyncConnection, AsyncDatabase
from zeroth.platform.storage.database import CoordinationTimeoutError, database_now

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
_UNSCOPED_WORKSPACE = object()

# The columns that constitute the fence itself. A fenced write may never touch
# them: doing so would let a displaced worker re-grant its own lease.
_FENCE_COLUMNS = frozenset(
    {"lease_worker_id", "lease_generation", "lease_acquired_at", "lease_expires_at"}
)

# The run-state columns a fenced write may set. An allowlist, not a denylist:
# a denylist has to anticipate every spelling of a forbidden name, and these
# names are interpolated into SQL, so anything unanticipated is both a fence
# bypass and an injection vector. Membership here is exact and case-sensitive,
# which is also how the schema declares them.
_FENCEABLE_COLUMNS = frozenset(
    {
        "status",
        "current_step",
        "current_node_ids",
        "pending_node_ids",
        "completed_steps",
        "artifacts",
        "channels",
        "final_output",
        "failure_state",
        "error",
        "metadata",
        "execution_history",
        "node_visit_counts",
        "condition_results",
        "audit_refs",
        "updated_at",
        "recovery_checkpoint_id",
        "failure_count",
    }
)


async def _database_now(connection: AsyncConnection, *, postgres: bool) -> datetime:
    return await database_now(connection, "postgres" if postgres else "sqlite")


def _new_worker_id() -> str:
    return uuid4().hex


def _scope_sql(
    tenant_id: str | None,
    workspace_id: str | None | object,
) -> tuple[str, tuple[object, ...]]:
    """Return one exact scope; omissions preserve only default/null compatibility."""
    if tenant_id is None:
        if workspace_id is not _UNSCOPED_WORKSPACE and workspace_id is not None:
            return "AND 1 = 0", ()
        tenant_id = "default"
    if workspace_id is _UNSCOPED_WORKSPACE:
        workspace_id = None
    workspace_scope = "null" if workspace_id is None else f"value:{workspace_id}"
    return (
        "AND tenant_id = ? AND workspace_scope = ?",
        (tenant_id, workspace_scope),
    )


class FencedRunWriteRejectedError(RuntimeError):
    """A fenced run-state write was refused because lease ownership moved.

    Raised by the run store when a save carries a fence (worker id plus lease
    generation) that no longer matches the row. The write did not land; the
    caller has been displaced and must stop, not retry.
    """

    def __init__(self, run_id: str, worker_id: str, generation: int) -> None:
        super().__init__(
            f"run {run_id}: state write fenced out — worker {worker_id} no longer "
            f"holds lease generation {generation}"
        )
        self.run_id = run_id
        self.worker_id = worker_id
        self.generation = generation


@dataclass(frozen=True, slots=True)
class LeaseClaimResult:
    """One claim outcome, including concurrency state from the same transaction."""

    run_id: str | None
    concurrency_saturated: bool
    active_count: int
    max_concurrency: int | None

    @property
    def utilization(self) -> float:
        """Return bounded active-slot utilization for audit and metrics."""
        if self.max_concurrency is None:
            return 0.0
        return min(1.0, self.active_count / self.max_concurrency)


@dataclass(frozen=True, slots=True)
class _ConcurrencyAvailability:
    """Shared-capacity snapshot read in the claim transaction."""

    slots: int | None
    active_count: int
    max_concurrency: int | None

    @property
    def saturated(self) -> bool:
        """Return whether the shared deployment has no execution slot."""
        return self.slots == 0

    def result(self, run_id: str | None) -> LeaseClaimResult:
        """Bind one run claim to the capacity snapshot that governed it."""
        return LeaseClaimResult(
            run_id=run_id,
            concurrency_saturated=self.saturated,
            active_count=self.active_count,
            max_concurrency=self.max_concurrency,
        )


@dataclass(frozen=True, slots=True)
class _OrphanClaimResult:
    """One bounded orphan scan, including why it returned no runs."""

    run_ids: tuple[str, ...]
    concurrency_saturated: bool


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

    async def claim_pending(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> str | None:
        """Atomically claim one PENDING run for this worker.

        Dispatches to ``_claim_pending_pg`` (Postgres) or
        ``_claim_pending_sqlite`` (SQLite) based on the database backend.

        Returns the run_id that was claimed, or None if no work is available.
        The claimed run's status is left as PENDING -- the worker transitions
        it to RUNNING once execution actually starts.
        """
        scope = (
            {}
            if tenant_id is None and workspace_id is _UNSCOPED_WORKSPACE
            else {"tenant_id": tenant_id, "workspace_id": workspace_id}
        )
        concurrency = {} if max_concurrency is None else {"max_concurrency": max_concurrency}
        if self._is_postgres():
            return await self._claim_pending_pg(
                deployment_ref,
                worker_id,
                **scope,
                **concurrency,
            )
        return await self._claim_pending_sqlite(
            deployment_ref,
            worker_id,
            **scope,
            **concurrency,
        )

    async def claim_pending_result(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> LeaseClaimResult:
        """Claim once and return saturation observed by this exact call."""
        scope = (
            {}
            if tenant_id is None and workspace_id is _UNSCOPED_WORKSPACE
            else {"tenant_id": tenant_id, "workspace_id": workspace_id}
        )
        concurrency = {} if max_concurrency is None else {"max_concurrency": max_concurrency}
        claim = (
            self._claim_pending_pg_result
            if self._is_postgres()
            else self._claim_pending_sqlite_result
        )
        return await claim(deployment_ref, worker_id, **scope, **concurrency)

    async def _claim_pending_sqlite(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> str | None:
        """Preserve the legacy run-id-only SQLite claim contract."""
        result = await self._claim_pending_sqlite_result(
            deployment_ref,
            worker_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            max_concurrency=max_concurrency,
        )
        return result.run_id

    async def _claim_pending_sqlite_result(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> LeaseClaimResult:
        """Claim using a guarded UPDATE ... RETURNING (SQLite).

        The previous shape selected a candidate, updated it, then re-read to
        check ``lease_worker_id`` matched. That verify is not a race check: two
        claimers sharing a worker id both saw their own id and both reported
        success. ``RETURNING`` makes the guard and the answer one statement, so
        exactly one caller gets a row back regardless of worker ids.
        """
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction(write_lock=max_concurrency is not None) as conn:
            if max_concurrency is not None:
                await self._lock_admission_scope(
                    conn,
                    deployment_ref,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            now = await _database_now(conn, postgres=self._is_postgres())
            expires_at = now + timedelta(seconds=self.lease_duration_seconds)
            availability = await self._available_concurrency_slots(
                conn,
                deployment_ref,
                now,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                max_concurrency=max_concurrency,
            )
            if availability.saturated:
                return availability.result(None)
            row = await conn.fetch_one(
                f"""
                SELECT run_id, tenant_id, workspace_id, workspace_scope FROM runs
                WHERE deployment_ref = ?
                  {scope_sql}
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                ORDER BY started_at ASC
                LIMIT 1
                """,
                (deployment_ref, *scope_params, _STATUS_PENDING, now.isoformat()),
            )
            if row is None:
                return availability.result(None)
            exact_scope_sql, exact_scope_params = _scope_sql(
                str(row["tenant_id"]), row["workspace_id"]
            )
            # The generation advances with the claim so a displaced worker's
            # writes can be told apart from the new owner's.
            won = await conn.fetch_one(
                f"""
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?,
                    lease_generation = lease_generation + 1
                WHERE run_id = ?
                  {exact_scope_sql}
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                RETURNING run_id
                """,
                (
                    worker_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                    row["run_id"],
                    *exact_scope_params,
                    _STATUS_PENDING,
                    now.isoformat(),
                ),
            )
        run_id = None if won is None else str(won["run_id"])
        return availability.result(run_id)

    async def _claim_pending_pg(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> str | None:
        """Preserve the legacy run-id-only PostgreSQL claim contract."""
        result = await self._claim_pending_pg_result(
            deployment_ref,
            worker_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            max_concurrency=max_concurrency,
        )
        return result.run_id

    async def _claim_pending_pg_result(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
    ) -> LeaseClaimResult:
        """Atomic claim using SELECT ... FOR UPDATE SKIP LOCKED (Postgres).

        Workers skip rows already being claimed by another worker.
        No verify step needed -- the lock is acquired at SELECT time.
        """
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction(write_lock=max_concurrency is not None) as conn:
            if max_concurrency is not None:
                await self._lock_admission_scope(
                    conn,
                    deployment_ref,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            now = await _database_now(conn, postgres=True)
            expires_at = now + timedelta(seconds=self.lease_duration_seconds)
            availability = await self._available_concurrency_slots(
                conn,
                deployment_ref,
                now,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                max_concurrency=max_concurrency,
            )
            if availability.saturated:
                return availability.result(None)
            row = await conn.fetch_one(
                f"""
                SELECT run_id, tenant_id, workspace_id, workspace_scope FROM runs
                WHERE deployment_ref = ?
                  {scope_sql}
                  AND status = ?
                  AND (lease_worker_id IS NULL OR lease_expires_at < ?)
                ORDER BY started_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (deployment_ref, *scope_params, _STATUS_PENDING, now.isoformat()),
            )
            if row is None:
                return availability.result(None)
            run_id = row["run_id"]
            exact_scope_sql, exact_scope_params = _scope_sql(
                str(row["tenant_id"]), row["workspace_id"]
            )
            await conn.execute(
                f"""
                UPDATE runs
                SET lease_worker_id = ?,
                    lease_acquired_at = ?,
                    lease_expires_at = ?,
                    lease_generation = lease_generation + 1
                WHERE run_id = ?
                  {exact_scope_sql}
                """,
                (
                    worker_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                    run_id,
                    *exact_scope_params,
                ),
            )
        return availability.result(str(run_id))

    async def _available_concurrency_slots(
        self,
        connection: AsyncConnection,
        deployment_ref: str,
        now: datetime,
        *,
        tenant_id: str | None,
        workspace_id: str | None | object,
        max_concurrency: int | None,
    ) -> _ConcurrencyAvailability:
        """Return same-transaction utilization under the admission lock."""
        if max_concurrency is None:
            return _ConcurrencyAvailability(None, 0, None)
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if tenant_id is None or workspace_id is _UNSCOPED_WORKSPACE:
            raise ValueError("distributed concurrency requires an exact tenant/workspace scope")
        now_value = now.isoformat()

        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        row = await connection.fetch_one(
            f"""SELECT COUNT(*) AS active_count FROM runs
                WHERE deployment_ref = ? {scope_sql}
                  AND lease_worker_id IS NOT NULL
                  AND lease_expires_at >= ?""",
            (deployment_ref, *scope_params, now_value),
        )
        active_count = 0 if row is None else int(row["active_count"])
        slots = max(0, max_concurrency - active_count)
        return _ConcurrencyAvailability(slots, active_count, max_concurrency)

    async def _lock_admission_scope(
        self,
        connection: AsyncConnection,
        deployment_ref: str,
        *,
        tenant_id: str | None,
        workspace_id: str | None | object,
    ) -> None:
        """Acquire the deployment admission lock before sampling decision time."""
        if tenant_id is None or workspace_id is _UNSCOPED_WORKSPACE:
            raise ValueError("distributed concurrency requires an exact tenant/workspace scope")
        workspace_scope = "null" if workspace_id is None else f"value:{workspace_id}"
        created_at = (
            "CAST(clock_timestamp() AS TEXT)" if self._is_postgres() else "CURRENT_TIMESTAMP"
        )
        await connection.execute(
            f"""INSERT INTO guardrail_admission_state
               (tenant_id, workspace_id, workspace_scope, deployment_ref, created_at)
               VALUES (?, ?, ?, ?, {created_at})
               ON CONFLICT (tenant_id, workspace_scope, deployment_ref) DO NOTHING""",
            (tenant_id, workspace_id, workspace_scope, deployment_ref),
        )
        lock_suffix = " FOR UPDATE" if self._is_postgres() else ""
        locked = await connection.fetch_one(
            """SELECT deployment_ref FROM guardrail_admission_state
               WHERE tenant_id = ? AND workspace_scope = ? AND deployment_ref = ?"""
            + lock_suffix,
            (tenant_id, workspace_scope, deployment_ref),
        )
        assert locked is not None

    async def claim_orphaned(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
        claim_limit: int | None = None,
    ) -> list[str]:
        """Claim all RUNNING runs with expired leases for this deployment.

        Called at worker startup to recover work abandoned by crashed workers.
        Sets ``recovery_checkpoint_id`` to the latest checkpoint for each
        claimed run so the worker knows where to resume.
        """
        result = await self.claim_orphaned_result(
            deployment_ref,
            worker_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            max_concurrency=max_concurrency,
            claim_limit=claim_limit,
        )
        return list(result.run_ids)

    async def claim_orphaned_result(
        self,
        deployment_ref: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
        max_concurrency: int | None = None,
        claim_limit: int | None = None,
    ) -> _OrphanClaimResult:
        """Claim expired RUNNING runs and distinguish saturation from exhaustion."""
        claimed: list[str] = []
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction(write_lock=max_concurrency is not None) as conn:
            if max_concurrency is not None:
                await self._lock_admission_scope(
                    conn,
                    deployment_ref,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            now = await _database_now(conn, postgres=self._is_postgres())
            expires_at = now + timedelta(seconds=self.lease_duration_seconds)
            availability = await self._available_concurrency_slots(
                conn,
                deployment_ref,
                now,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                max_concurrency=max_concurrency,
            )
            if claim_limit is not None and claim_limit < 1:
                raise ValueError("claim_limit must be positive")
            if availability.saturated:
                waiting = await conn.fetch_one(
                    f"""SELECT 1 AS waiting FROM runs
                        WHERE deployment_ref = ?
                          {scope_sql}
                          AND status = ?
                          AND lease_worker_id IS NOT NULL
                          AND lease_expires_at < ?
                        LIMIT 1""",
                    (deployment_ref, *scope_params, _STATUS_RUNNING, now.isoformat()),
                )
                return _OrphanClaimResult((), waiting is not None)
            available = availability.slots
            limit = claim_limit if available is None else available
            if claim_limit is not None and available is not None:
                limit = min(claim_limit, available)
            limit_sql = "" if limit is None else f" LIMIT {limit}"
            rows = await conn.fetch_all(
                f"""
                SELECT run_id, tenant_id, workspace_id, workspace_scope FROM runs
                WHERE deployment_ref = ?
                  {scope_sql}
                  AND status = ?
                  AND lease_worker_id IS NOT NULL
                  AND lease_expires_at < ?
                ORDER BY started_at ASC{limit_sql}
                """,
                (deployment_ref, *scope_params, _STATUS_RUNNING, now.isoformat()),
            )
            for row in rows:
                run_id = row["run_id"]
                exact_scope_sql, exact_scope_params = _scope_sql(
                    str(row["tenant_id"]), row["workspace_id"]
                )
                # Find the latest checkpoint for this run.
                cp_row = await conn.fetch_one(
                    f"""
                    SELECT checkpoint_id FROM run_checkpoints
                    WHERE run_id = ?
                      {exact_scope_sql}
                    ORDER BY checkpoint_order DESC
                    LIMIT 1
                    """,
                    (run_id, *exact_scope_params),
                )
                recovery_checkpoint_id = cp_row["checkpoint_id"] if cp_row else None
                # Guarded on the row still being expired: the SELECT above is
                # not a lock, so another worker can reclaim between the two
                # statements and both would otherwise report the same run.
                won = await conn.fetch_one(
                    f"""
                    UPDATE runs
                    SET lease_worker_id = ?,
                        lease_acquired_at = ?,
                        lease_expires_at = ?,
                        recovery_checkpoint_id = ?,
                        lease_generation = lease_generation + 1
                    WHERE run_id = ?
                      {exact_scope_sql}
                      AND status = ?
                      AND lease_expires_at < ?
                    RETURNING run_id
                    """,
                    (
                        worker_id,
                        now.isoformat(),
                        expires_at.isoformat(),
                        recovery_checkpoint_id,
                        run_id,
                        *exact_scope_params,
                        _STATUS_RUNNING,
                        now.isoformat(),
                    ),
                )
                if won is not None:
                    claimed.append(run_id)
        return _OrphanClaimResult(tuple(claimed), False)

    # ---------------------------------------------------------------------------
    # Lease maintenance
    # ---------------------------------------------------------------------------

    async def renew_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        generation: int | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
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
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        try:
            async with self.database.transaction(write_lock=True) as conn:
                row = await conn.fetch_one(
                    f"""SELECT tenant_id, workspace_id, deployment_ref
                         FROM runs WHERE run_id = ? {scope_sql}""",
                    (run_id, *scope_params),
                )
                if row is None:
                    return False
                exact_scope_sql, exact_scope_params = _scope_sql(
                    str(row["tenant_id"]), row["workspace_id"]
                )
                await self._lock_admission_scope(
                    conn,
                    str(row["deployment_ref"]),
                    tenant_id=str(row["tenant_id"]),
                    workspace_id=row["workspace_id"],
                )
                current_time = await _database_now(conn, postgres=self._is_postgres())
                generation_sql = "" if generation is None else "AND lease_generation = ?"
                params: tuple[object, ...] = (
                    (current_time + timedelta(seconds=self.lease_duration_seconds)).isoformat(),
                    run_id,
                    *exact_scope_params,
                    worker_id,
                    current_time.isoformat(),
                )
                if generation is not None:
                    params += (generation,)
                renewed = await conn.fetch_one(
                    f"""
                    UPDATE runs
                    SET lease_expires_at = ?
                    WHERE run_id = ?
                      {exact_scope_sql}
                      AND lease_worker_id = ?
                      AND lease_expires_at >= ?
                      {generation_sql}
                    RETURNING run_id
                    """,
                    params,
                )
        except CoordinationTimeoutError:
            return False
        return renewed is not None

    async def current_generation(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> int | None:
        """The run's current lease generation, or None if the run is unknown."""
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                f"SELECT lease_generation FROM runs WHERE run_id = ? {scope_sql}",
                (run_id, *scope_params),
            )
        return None if row is None else int(row["lease_generation"])

    async def current_holder(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> str | None:
        """The worker id currently holding the run's lease, or None.

        A write fence is only meaningful for the worker that actually holds the
        lease; installing one without ownership would reject every save.
        """
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                f"SELECT lease_worker_id FROM runs WHERE run_id = ? {scope_sql}",
                (run_id, *scope_params),
            )
        return None if row is None else row["lease_worker_id"]

    async def commit_fenced(
        self,
        run_id: str,
        worker_id: str,
        *,
        generation: int,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
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
        rejected = sorted(set(columns) - _FENCEABLE_COLUMNS)
        if rejected:
            # Allowlisted, not denylisted. A denylist would have to anticipate
            # every spelling -- "LEASE_WORKER_ID", a quoted alias, a whole SQL
            # fragment -- and these names reach the statement text, so an
            # unanticipated one is both a fence bypass and an injection vector.
            fence_hit = sorted(
                name for name in rejected if name.strip('"').lower() in _FENCE_COLUMNS
            )
            detail = (
                f"may not write lease columns: {fence_hit}"
                if fence_hit
                else f"unknown run columns: {rejected}"
            )
            raise ValueError(f"commit_fenced {detail}")
        # Quoted even though every name is allowlisted: the allowlist is the
        # security boundary, and quoting keeps a name that merely *looks*
        # like SQL from ever being read as SQL if that list is widened.
        assignments = ", ".join(f'"{name}" = ?' for name in columns)
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            written = await conn.fetch_one(
                f"""
                UPDATE runs
                SET {assignments}
                WHERE run_id = ?
                  {scope_sql}
                  AND lease_worker_id = ?
                  AND lease_generation = ?
                RETURNING run_id
                """,
                (
                    *columns.values(),
                    run_id,
                    *scope_params,
                    worker_id,
                    generation,
                ),
            )
        applied = written is not None
        if not applied and metrics_collector is not None:
            metrics_collector.increment("zeroth_lease_fencing_rejected_total")
        return applied

    async def release_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> None:
        """Clear the lease columns after a run finishes (success or failure)."""
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE runs
                SET lease_worker_id = NULL,
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    recovery_checkpoint_id = NULL
                WHERE run_id = ?
                  {scope_sql}
                  AND lease_worker_id = ?
                """,
                (run_id, *scope_params, worker_id),
            )

    async def expire_recovery_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        generation: int,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> bool:
        """Make owned recovery work immediately reclaimable without losing its checkpoint."""
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            now = await _database_now(conn, postgres=self._is_postgres())
            written = await conn.fetch_one(
                f"""
                UPDATE runs
                SET lease_expires_at = ?
                WHERE run_id = ?
                  {scope_sql}
                  AND status = ?
                  AND lease_worker_id = ?
                  AND lease_generation = ?
                RETURNING run_id
                """,
                (
                    (now - timedelta(microseconds=1)).isoformat(),
                    run_id,
                    *scope_params,
                    _STATUS_RUNNING,
                    worker_id,
                    generation,
                ),
            )
        return written is not None

    async def clear_lease(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> None:
        """Clear the lease columns regardless of the current lease owner."""
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE runs
                SET lease_worker_id = NULL,
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    recovery_checkpoint_id = NULL
                WHERE run_id = ?
                  {scope_sql}
                """,
                (run_id, *scope_params),
            )

    async def get_recovery_checkpoint_id(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> str | None:
        """Return the recovery_checkpoint_id stored on the run, if any."""
        scope_sql, scope_params = _scope_sql(tenant_id, workspace_id)
        async with self.database.transaction() as conn:
            row = await conn.fetch_one(
                f"SELECT recovery_checkpoint_id FROM runs WHERE run_id = ? {scope_sql}",
                (run_id, *scope_params),
            )
        return row["recovery_checkpoint_id"] if row else None
