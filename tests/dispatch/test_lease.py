"""Tests for the backend-conditional LeaseManager."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.dispatch.lease import LeaseManager, _HAS_PG
from zeroth.runs import RunRepository, RunStatus
from zeroth.storage import SQLiteDatabase

DEPLOYMENT = "test-deployment"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


def _create_pending_run(run_repo: RunRepository) -> str:
    """Create a PENDING run and return its run_id."""
    from zeroth.runs.models import Run
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    persisted = run_repo.create(run)
    return persisted.run_id


# ---------------------------------------------------------------------------
# SQLite path tests (existing)
# ---------------------------------------------------------------------------


def test_claim_pending_returns_none_when_empty(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    RunRepository(sqlite_db)

    result = manager.claim_pending(DEPLOYMENT, WORKER_A)

    assert result is None


def test_claim_pending_claims_oldest_run(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)

    claimed = manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert claimed == run_id

    # Second claim should return None — run is already leased.
    second = manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert second is None


def test_claim_pending_sets_lease_columns(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    manager.claim_pending(DEPLOYMENT, WORKER_A)

    # Directly inspect the database for lease columns.
    with sqlite_db.transaction() as conn:
        row = conn.execute(
            "SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row["lease_worker_id"] == WORKER_A


def test_release_clears_lease_columns(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    manager.claim_pending(DEPLOYMENT, WORKER_A)
    manager.release_lease(run_id, WORKER_A)

    # After release the run should be claimable again.
    reclaimed = manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert reclaimed == run_id


def test_renew_lease_returns_true_for_owner(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = manager.renew_lease(run_id, WORKER_A)
    assert result is True


def test_renew_lease_returns_false_for_non_owner(sqlite_db: SQLiteDatabase) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = manager.renew_lease(run_id, WORKER_B)
    assert result is False


def test_concurrent_claims_do_not_overlap(sqlite_db: SQLiteDatabase) -> None:
    """Two concurrent workers should each claim at most one distinct run."""
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id_a = _create_pending_run(run_repo)
    run_id_b = _create_pending_run(run_repo)

    claimed_a = manager.claim_pending(DEPLOYMENT, WORKER_A)
    claimed_b = manager.claim_pending(DEPLOYMENT, WORKER_B)

    # Each worker should get a different run, and together they cover both.
    assert {claimed_a, claimed_b} == {run_id_a, run_id_b}


def test_claim_orphaned_finds_running_runs_with_expired_leases(
    sqlite_db: SQLiteDatabase,
) -> None:
    manager = LeaseManager(sqlite_db, lease_duration_seconds=60)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    # Claim then immediately expire the lease in DB.
    manager.claim_pending(DEPLOYMENT, WORKER_A)
    run_repo.transition(run_id, RunStatus.RUNNING)
    with sqlite_db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )

    orphans = manager.claim_orphaned(DEPLOYMENT, WORKER_B)
    assert run_id in orphans


def test_get_recovery_checkpoint_id_returns_none_when_no_checkpoint(
    sqlite_db: SQLiteDatabase,
) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = _create_pending_run(run_repo)
    result = manager.get_recovery_checkpoint_id(run_id)
    assert result is None


# ---------------------------------------------------------------------------
# SQLite fallback test
# ---------------------------------------------------------------------------


def test_claim_pending_sqlite_fallback(sqlite_db: SQLiteDatabase) -> None:
    """With a non-Postgres database, verify _claim_pending_sqlite is called."""
    manager = LeaseManager(sqlite_db)

    assert manager._is_postgres() is False

    # Patch on the class (slots=True prevents instance-level patching)
    with patch.object(LeaseManager, "_claim_pending_sqlite", return_value=None) as mock_sqlite:
        manager.claim_pending(DEPLOYMENT, WORKER_A)
        mock_sqlite.assert_called_once_with(DEPLOYMENT, WORKER_A)


# ---------------------------------------------------------------------------
# Backend detection tests
# ---------------------------------------------------------------------------


def test_is_postgres_detection_with_sqlite(sqlite_db: SQLiteDatabase) -> None:
    """_is_postgres returns False for SQLiteDatabase instances."""
    manager = LeaseManager(sqlite_db)
    assert manager._is_postgres() is False


@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
def test_is_postgres_detection_with_pg() -> None:
    """_is_postgres returns True for AsyncPostgresDatabase instances."""
    from zeroth.storage.async_postgres import AsyncPostgresDatabase

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


@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
def test_claim_pending_pg_uses_skip_locked() -> None:
    """When database is AsyncPostgresDatabase, _claim_pending_pg is called."""
    from zeroth.storage.async_postgres import AsyncPostgresDatabase

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)
    manager = LeaseManager(pg_db)  # type: ignore[arg-type]

    with patch.object(LeaseManager, "_claim_pending_pg", return_value="test-run") as mock_pg:
        result = manager.claim_pending(DEPLOYMENT, WORKER_A)
        mock_pg.assert_called_once_with(DEPLOYMENT, WORKER_A)
        assert result == "test-run"


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
async def test_claim_pending_pg_returns_none_when_no_work() -> None:
    """Postgres claim returns None when no pending rows found."""
    from zeroth.storage.async_postgres import AsyncPostgresDatabase

    mock_conn = AsyncMock()
    mock_conn.fetch_one = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)

    # Mock the transaction context manager
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_tx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(pg_db, "transaction", return_value=mock_tx):
        manager = LeaseManager(pg_db)  # type: ignore[arg-type]
        result = await manager._claim_pending_pg_async(DEPLOYMENT, WORKER_A)
        assert result is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_PG, reason="psycopg not installed")
async def test_claim_pending_pg_returns_run_id_on_success() -> None:
    """Postgres claim returns run_id when a pending row is found."""
    from zeroth.storage.async_postgres import AsyncPostgresDatabase

    mock_conn = AsyncMock()
    mock_conn.fetch_one = AsyncMock(return_value={"run_id": "test-123"})
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    pg_db = AsyncPostgresDatabase(pool=mock_pool)

    # Mock the transaction context manager
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_tx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(pg_db, "transaction", return_value=mock_tx):
        manager = LeaseManager(pg_db)  # type: ignore[arg-type]
        result = await manager._claim_pending_pg_async(DEPLOYMENT, WORKER_A)
        assert result == "test-123"

        # Verify the UPDATE was called with the run_id
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "UPDATE runs" in call_args[0][0]
        assert "test-123" in call_args[0][1]
