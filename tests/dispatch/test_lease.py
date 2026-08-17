"""Tests for the backend-conditional LeaseManager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import zeroth.platform.dispatch.lease as lease_module
from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import _HAS_PG, LeaseManager
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage.database import CoordinationTimeoutError
from zeroth.runtime.runs import RunStatus

DEPLOYMENT = "test-deployment"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


async def _create_pending_run(run_repo: RunRepository) -> str:
    """Create a PENDING run and return its run_id."""
    from zeroth.runtime.runs import Run

    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    persisted = await run_repo.create(run)
    return persisted.run_id


async def _assert_fast_replica_cannot_overallocate(database, monkeypatch) -> None:
    old_owner = LeaseManager(database, lease_duration_seconds=60)
    fast_replica = LeaseManager(database, lease_duration_seconds=60)
    run_repo = RunRepository.for_default_compatibility(database)
    original_run = await _create_pending_run(run_repo)
    replacement_run = await _create_pending_run(run_repo)
    scope = {"tenant_id": "default", "workspace_id": None, "max_concurrency": 1}

    assert await old_owner.claim_pending(DEPLOYMENT, WORKER_A, **scope) == original_run
    await run_repo.transition(original_run, RunStatus.RUNNING)
    monkeypatch.setattr(
        lease_module,
        "_utc_now",
        lambda: datetime.now(UTC) + timedelta(days=1),
        raising=False,
    )

    replacement = await fast_replica.claim_pending_result(DEPLOYMENT, WORKER_B, **scope)
    renewed = await old_owner.renew_lease(original_run, WORKER_A, generation=1)
    async with database.transaction() as connection:
        leased = await connection.fetch_one(
            "SELECT COUNT(*) AS count FROM runs WHERE lease_worker_id IS NOT NULL"
        )

    assert replacement_run != original_run
    assert replacement.run_id is None
    assert replacement.concurrency_saturated is True
    assert replacement.active_count == 1
    assert renewed is True
    assert leased is not None and int(leased["count"]) == 1


# ---------------------------------------------------------------------------
# SQLite path tests (existing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_pending_returns_none_when_empty(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    RunRepository.for_default_compatibility(sqlite_db)

    result = await manager.claim_pending(DEPLOYMENT, WORKER_A)

    assert result is None


@pytest.mark.asyncio
async def test_claim_pending_claims_oldest_run(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)

    claimed = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert claimed == run_id

    # Second claim should return None — run is already leased.
    second = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert second is None


@pytest.mark.asyncio
async def test_claim_pending_sets_lease_columns(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    # Directly inspect the database for lease columns.
    async with sqlite_db.transaction() as conn:
        row = await conn.fetch_one("SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,))
    assert row["lease_worker_id"] == WORKER_A


@pytest.mark.asyncio
async def test_release_clears_lease_columns(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    await manager.release_lease(run_id, WORKER_A, generation=1)

    # After release the run should be claimable again.
    reclaimed = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert reclaimed == run_id


@pytest.mark.asyncio
async def test_renew_lease_returns_true_for_owner(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = await manager.renew_lease(run_id, WORKER_A)
    assert result is True


@pytest.mark.asyncio
async def test_renew_lease_returns_false_for_non_owner(sqlite_db: AsyncSQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = await manager.renew_lease(run_id, WORKER_B)
    assert result is False


@pytest.mark.asyncio
async def test_renewal_coordination_timeout_fails_closed(
    sqlite_db: AsyncSQLiteDatabase,
    monkeypatch,
) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    run_id = await _create_pending_run(run_repo)
    assert await manager.claim_pending(DEPLOYMENT, WORKER_A) == run_id
    transaction = AsyncMock()
    transaction.__aenter__.side_effect = CoordinationTimeoutError("coordination lock")
    monkeypatch.setattr(sqlite_db, "transaction", lambda **_kwargs: transaction)

    assert await manager.renew_lease(run_id, WORKER_A) is False


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed_after_slot_reallocation(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    old_owner = LeaseManager(sqlite_db)
    new_owner = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    expired_run = await _create_pending_run(run_repo)
    replacement_run = await _create_pending_run(run_repo)
    scope = {"tenant_id": "default", "workspace_id": None, "max_concurrency": 1}

    assert await old_owner.claim_pending(DEPLOYMENT, WORKER_A, **scope) == expired_run
    await run_repo.transition(expired_run, RunStatus.RUNNING)
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", expired_run),
        )
    assert await new_owner.claim_pending(DEPLOYMENT, WORKER_B, **scope) == replacement_run

    assert await old_owner.renew_lease(expired_run, WORKER_A, generation=1) is False
    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT lease_expires_at FROM runs WHERE run_id = ?",
            (expired_run,),
        )
    assert row["lease_expires_at"] == "2000-01-01T00:00:00+00:00"


@requires_docker
@pytest.mark.asyncio
async def test_renewal_reallocation_is_serialized_on_both_backends(
    dual_database,
    monkeypatch,
) -> None:
    old_owner = LeaseManager(dual_database, lease_duration_seconds=60)
    new_owner = LeaseManager(dual_database, lease_duration_seconds=60)
    run_repo = RunRepository.for_default_compatibility(dual_database)
    expired_run = await _create_pending_run(run_repo)
    replacement_run = await _create_pending_run(run_repo)
    scope = {"tenant_id": "default", "workspace_id": None, "max_concurrency": 1}
    expiry = datetime(2030, 1, 1, tzinfo=UTC)
    before_expiry = expiry - timedelta(seconds=1)
    after_expiry = expiry + timedelta(seconds=1)

    assert await old_owner.claim_pending(DEPLOYMENT, WORKER_A, **scope) == expired_run
    await run_repo.transition(expired_run, RunStatus.RUNNING)
    async with dual_database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            (expiry.isoformat(), expired_run),
        )

    renewal_sampled = asyncio.Event()
    release_renewal = asyncio.Event()

    async def controlled_database_now(connection, *, postgres):
        del connection
        del postgres
        if asyncio.current_task().get_name() == "stale-renewal":
            renewal_sampled.set()
            await release_renewal.wait()
            return before_expiry
        return after_expiry

    monkeypatch.setattr(lease_module, "_database_now", controlled_database_now)
    renewal = asyncio.create_task(
        old_owner.renew_lease(expired_run, WORKER_A, generation=1),
        name="stale-renewal",
    )
    await asyncio.wait_for(renewal_sampled.wait(), timeout=1)
    replacement = asyncio.create_task(new_owner.claim_pending_result(DEPLOYMENT, WORKER_B, **scope))
    await asyncio.wait({replacement}, timeout=0.1)
    release_renewal.set()

    renewed, replacement_result = await asyncio.wait_for(
        asyncio.gather(renewal, replacement),
        timeout=2,
    )
    async with dual_database.transaction() as connection:
        leased = await connection.fetch_one(
            "SELECT COUNT(*) AS count FROM runs "
            "WHERE lease_worker_id IS NOT NULL AND lease_expires_at >= ?",
            (after_expiry.isoformat(),),
        )

    assert renewed is True
    assert replacement_result.run_id is None, replacement_run
    assert leased is not None and int(leased["count"]) == 1


@pytest.mark.asyncio
async def test_fast_replica_clock_cannot_overallocate_live_lease(
    sqlite_db: AsyncSQLiteDatabase,
    monkeypatch,
) -> None:
    await _assert_fast_replica_cannot_overallocate(sqlite_db, monkeypatch)


@requires_docker
@pytest.mark.asyncio
async def test_fast_replica_clock_cannot_overallocate_live_lease_on_both_backends(
    dual_database,
    monkeypatch,
) -> None:
    await _assert_fast_replica_cannot_overallocate(dual_database, monkeypatch)


@pytest.mark.asyncio
async def test_concurrent_claims_do_not_overlap(sqlite_db: AsyncSQLiteDatabase) -> None:
    """Two concurrent workers should each claim at most one distinct run."""
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id_a = await _create_pending_run(run_repo)
    run_id_b = await _create_pending_run(run_repo)

    claimed_a = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    claimed_b = await manager.claim_pending(DEPLOYMENT, WORKER_B)

    # Each worker should get a different run, and together they cover both.
    assert {claimed_a, claimed_b} == {run_id_a, run_id_b}


@pytest.mark.asyncio
async def test_claim_orphaned_finds_running_runs_with_expired_leases(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    manager = LeaseManager(sqlite_db, lease_duration_seconds=60)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    # Claim then immediately expire the lease in DB.
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    await run_repo.transition(run_id, RunStatus.RUNNING)
    async with sqlite_db.transaction() as conn:
        await conn.execute(
            "UPDATE runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )

    orphans = await manager.claim_orphaned(DEPLOYMENT, WORKER_B)
    assert run_id in orphans


@requires_docker
async def test_orphan_scan_is_exhausted_when_capacity_is_full_without_an_orphan(
    dual_database,
) -> None:
    manager = LeaseManager(dual_database)
    run_repo = RunRepository.for_default_compatibility(dual_database)
    run_id = await _create_pending_run(run_repo)
    scope = {"tenant_id": "default", "workspace_id": None, "max_concurrency": 1}

    assert await manager.claim_pending(DEPLOYMENT, WORKER_A, **scope) == run_id
    await run_repo.transition(run_id, RunStatus.RUNNING)

    result = await manager.claim_orphaned_result(
        DEPLOYMENT,
        WORKER_B,
        claim_limit=1,
        **scope,
    )

    assert result.run_ids == ()
    assert result.concurrency_saturated is False


@pytest.mark.asyncio
async def test_get_recovery_checkpoint_id_returns_none_when_no_checkpoint(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository.for_default_compatibility(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    result = await manager.get_recovery_checkpoint_id(run_id)
    assert result is None


# ---------------------------------------------------------------------------
# SQLite fallback test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_pending_sqlite_fallback(sqlite_db: AsyncSQLiteDatabase) -> None:
    """With a non-Postgres database, verify _claim_pending_sqlite is called."""
    manager = LeaseManager(sqlite_db)

    assert manager._is_postgres() is False

    # Patch on the class (slots=True prevents instance-level patching)
    with patch.object(
        LeaseManager, "_claim_pending_sqlite", new_callable=AsyncMock, return_value=None
    ) as mock_sqlite:
        await manager.claim_pending(DEPLOYMENT, WORKER_A)
        mock_sqlite.assert_called_once_with(DEPLOYMENT, WORKER_A)


# ---------------------------------------------------------------------------
# Backend detection tests
# ---------------------------------------------------------------------------


def test_is_postgres_detection_with_sqlite(sqlite_db: AsyncSQLiteDatabase) -> None:
    """_is_postgres returns False for AsyncSQLiteDatabase instances."""
    manager = LeaseManager(sqlite_db)
    assert manager._is_postgres() is False


@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
def test_is_postgres_detection_with_pg() -> None:
    """_is_postgres returns True for AsyncPostgresDatabase instances."""
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)
    manager = LeaseManager(pg_db)  # type: ignore[arg-type]
    assert manager._is_postgres() is True


def test_is_postgres_detection_with_mock_non_pg() -> None:
    """_is_postgres returns False for non-Postgres AsyncDatabase implementations."""
    mock_db = MagicMock(spec=[])  # No AsyncPostgresDatabase attributes
    manager = LeaseManager(mock_db)  # type: ignore[arg-type]
    assert manager._is_postgres() is False


# ---------------------------------------------------------------------------
# Postgres path tests (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
async def test_claim_pending_pg_uses_skip_locked() -> None:
    """When database is AsyncPostgresDatabase, _claim_pending_pg is called."""
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)
    manager = LeaseManager(pg_db)  # type: ignore[arg-type]

    with patch.object(
        LeaseManager, "_claim_pending_pg", new_callable=AsyncMock, return_value="test-run"
    ) as mock_pg:
        result = await manager.claim_pending(DEPLOYMENT, WORKER_A)
        mock_pg.assert_called_once_with(DEPLOYMENT, WORKER_A)
        assert result == "test-run"


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
async def test_claim_pending_pg_returns_none_when_no_work() -> None:
    """Postgres claim returns None when no pending rows found."""
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    mock_conn = AsyncMock()
    mock_conn.fetch_one = AsyncMock(
        side_effect=[{"current_time": datetime(2026, 8, 16, tzinfo=UTC)}, None]
    )

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)

    # Mock the transaction context manager
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_tx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(pg_db, "transaction", return_value=mock_tx):
        manager = LeaseManager(pg_db)  # type: ignore[arg-type]
        result = await manager._claim_pending_pg(DEPLOYMENT, WORKER_A)
        assert result is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
async def test_claim_pending_pg_returns_run_id_on_success() -> None:
    """Postgres claim returns run_id when a pending row is found."""
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

    mock_conn = AsyncMock()
    mock_conn.fetch_one = AsyncMock(
        side_effect=[
            {"current_time": datetime(2026, 8, 16, tzinfo=UTC)},
            {
                "run_id": "test-123",
                "tenant_id": "default",
                "workspace_id": None,
                "workspace_scope": "null",
            },
        ]
    )
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)

    # Mock the transaction context manager
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_tx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(pg_db, "transaction", return_value=mock_tx):
        manager = LeaseManager(pg_db)  # type: ignore[arg-type]
        result = await manager._claim_pending_pg(DEPLOYMENT, WORKER_A)
        assert result == "test-123"

        # Verify the UPDATE was called with the run_id
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "UPDATE runs" in call_args[0][0]
        assert "test-123" in call_args[0][1]


def test_lease_status_literals_match_the_run_status_enum() -> None:
    """The lease SQL speaks the runs-table column contract, not the enum.

    The platform layer sits below the run domain, so ``lease.py`` carries the
    persisted status strings as module constants; this pin fails if the run
    domain ever changes the persisted vocabulary.
    """
    from zeroth.contracts.governed import RunStatus
    from zeroth.platform.dispatch import lease

    assert RunStatus.PENDING.value == lease._STATUS_PENDING
    assert RunStatus.RUNNING.value == lease._STATUS_RUNNING
