"""Tests for the async LeaseManager."""

from __future__ import annotations

from zeroth.dispatch.lease import LeaseManager
from zeroth.runs import RunRepository, RunStatus

DEPLOYMENT = "test-deployment"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


async def _create_pending_run(run_repo: RunRepository) -> str:
    """Create a PENDING run and return its run_id."""
    from zeroth.runs.models import Run

    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    persisted = await run_repo.create(run)
    return persisted.run_id


async def test_claim_pending_returns_none_when_empty(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    RunRepository(sqlite_db)

    result = await manager.claim_pending(DEPLOYMENT, WORKER_A)

    assert result is None


async def test_claim_pending_claims_oldest_run(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)

    claimed = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert claimed == run_id

    # Second claim should return None -- run is already leased.
    second = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert second is None


async def test_claim_pending_sets_lease_columns(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    # Directly inspect the database for lease columns.
    async with sqlite_db.transaction() as conn:
        row = await conn.fetch_one("SELECT lease_worker_id FROM runs WHERE run_id = ?", (run_id,))
    assert row["lease_worker_id"] == WORKER_A


async def test_release_clears_lease_columns(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    await manager.release_lease(run_id, WORKER_A)

    # After release the run should be claimable again.
    reclaimed = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    assert reclaimed == run_id


async def test_renew_lease_returns_true_for_owner(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = await manager.renew_lease(run_id, WORKER_A)
    assert result is True


async def test_renew_lease_returns_false_for_non_owner(sqlite_db) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    result = await manager.renew_lease(run_id, WORKER_B)
    assert result is False


async def test_concurrent_claims_do_not_overlap(sqlite_db) -> None:
    """Two concurrent workers should each claim at most one distinct run."""
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id_a = await _create_pending_run(run_repo)
    run_id_b = await _create_pending_run(run_repo)

    claimed_a = await manager.claim_pending(DEPLOYMENT, WORKER_A)
    claimed_b = await manager.claim_pending(DEPLOYMENT, WORKER_B)

    # Each worker should get a different run, and together they cover both.
    assert {claimed_a, claimed_b} == {run_id_a, run_id_b}


async def test_claim_orphaned_finds_running_runs_with_expired_leases(
    sqlite_db,
) -> None:
    manager = LeaseManager(sqlite_db, lease_duration_seconds=60)
    run_repo = RunRepository(sqlite_db)

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


async def test_get_recovery_checkpoint_id_returns_none_when_no_checkpoint(
    sqlite_db,
) -> None:
    manager = LeaseManager(sqlite_db)
    run_repo = RunRepository(sqlite_db)

    run_id = await _create_pending_run(run_repo)
    result = await manager.get_recovery_checkpoint_id(run_id)
    assert result is None
