"""Lease generations, stale-write fencing, and prompt stop on ownership loss.

ZER-26 R6/R7/R11. The contention and recovery cases run through the
``dual_database`` fixture so claim, renewal loss, takeover and stale-write
rejection are each proven on SQLite *and* Postgres -- the two backends resolve
contention by different mechanisms (timestamp-expiry verify vs SKIP LOCKED), so
one passing does not imply the other.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import LeaseManager
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.runtime.runs import Run, RunStatus

DEPLOYMENT = "fencing-deployment"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


async def _pending_run(db) -> str:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    return (await RunRepository(db).create(run)).run_id


async def _expire_lease(db, run_id: str) -> None:
    """Drive the lease into the past so another worker may reclaim it."""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run_id),
        )


# ---------------------------------------------------------------------------
# R6 / R11 -- generations and stale-write rejection, on both backends
# ---------------------------------------------------------------------------


@requires_docker
class TestLeaseFencingDualBackend:
    async def test_claim_advances_the_generation(self, dual_database) -> None:
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)

        assert await manager.current_generation(run_id) == 0
        claimed = await manager.claim_pending(DEPLOYMENT, WORKER_A)

        assert claimed == run_id
        assert await manager.current_generation(run_id) == 1

    async def test_takeover_advances_the_generation_again(self, dual_database) -> None:
        """Each ownership transfer must produce a strictly newer generation.

        Reusing a generation would make the previous owner's writes
        indistinguishable from the new owner's -- the fence would admit both.
        """
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)
        await _expire_lease(dual_database, run_id)

        await manager.claim_pending(DEPLOYMENT, WORKER_B)

        assert await manager.current_generation(run_id) == 2

    async def test_renewal_is_qualified_by_generation(self, dual_database) -> None:
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)

        assert await manager.renew_lease(run_id, WORKER_A, generation=1) is True
        assert await manager.renew_lease(run_id, WORKER_A, generation=0) is False

    async def test_renewal_reports_loss_after_takeover(self, dual_database) -> None:
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)
        await _expire_lease(dual_database, run_id)
        await manager.claim_pending(DEPLOYMENT, WORKER_B)

        assert await manager.renew_lease(run_id, WORKER_A, generation=1) is False
        assert await manager.renew_lease(run_id, WORKER_B, generation=2) is True

    async def test_stale_generation_write_is_rejected(self, dual_database) -> None:
        """The core fencing guarantee, proven by an actual attempted write.

        Worker A is displaced, then tries to commit under its old generation.
        The write must not land -- checking ownership separately would leave a
        window between the check and the write, so the fence is part of the
        UPDATE predicate itself.
        """
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)
        await _expire_lease(dual_database, run_id)
        await manager.claim_pending(DEPLOYMENT, WORKER_B)

        applied = await manager.commit_fenced(
            run_id,
            WORKER_A,
            generation=1,
            current_step="stale-step",
        )

        assert applied is False
        run = await RunRepository(dual_database).get(run_id)
        assert run is not None
        assert run.current_step != "stale-step"

    async def test_current_generation_write_is_applied(self, dual_database) -> None:
        """The positive control: fencing must not block the legitimate owner."""
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)

        applied = await manager.commit_fenced(
            run_id,
            WORKER_A,
            generation=1,
            current_step="live-step",
        )

        assert applied is True
        run = await RunRepository(dual_database).get(run_id)
        assert run is not None
        assert run.current_step == "live-step"

    async def test_orphan_reclaim_advances_the_generation(self, dual_database) -> None:
        manager = LeaseManager(dual_database)
        run_id = await _pending_run(dual_database)
        await manager.claim_pending(DEPLOYMENT, WORKER_A)
        await RunRepository(dual_database).transition(run_id, RunStatus.RUNNING)
        await _expire_lease(dual_database, run_id)

        reclaimed = await manager.claim_orphaned(DEPLOYMENT, WORKER_B)

        assert reclaimed == [run_id]
        assert await manager.current_generation(run_id) == 2
        assert await manager.renew_lease(run_id, WORKER_A, generation=1) is False


# ---------------------------------------------------------------------------
# R7 -- losing the lease stops local execution
# ---------------------------------------------------------------------------


class _StallingOrchestrator:
    """Drives a run that never finishes on its own, so only a cancel ends it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.completed = False

    async def _drive(self, graph, run):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return run

    async def resume_graph(self, graph, run_id: str):
        return None

    @property
    def approval_service(self):
        return None


class _FakeGraph:
    nodes: list = []
    entry_step: str = "start"


@pytest.mark.asyncio
async def test_lease_loss_cancels_the_running_execution(sqlite_db) -> None:
    """Ownership loss must stop the work, not merely be logged.

    Before this, ``_renewal_loop`` observed the loss, logged a warning and
    returned -- leaving the displaced worker driving the run to completion
    alongside its new owner.
    """
    run_repo = RunRepository(sqlite_db)
    manager = LeaseManager(sqlite_db, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)

    # Another worker takes over while this one is mid-flight.
    await _expire_lease(sqlite_db, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]

    await asyncio.wait_for(task, timeout=10)

    assert orchestrator.cancelled is True
    assert orchestrator.completed is False


@pytest.mark.asyncio
async def test_lease_loss_does_not_mark_the_run_failed(sqlite_db) -> None:
    """The displaced worker must not write a verdict on the new owner's run.

    Marking FAILED here would be a stale write with real consequences: the run
    is not failed, it simply belongs to somebody else now.
    """
    run_repo = RunRepository(sqlite_db)
    manager = LeaseManager(sqlite_db, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(sqlite_db, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    run = await run_repo.get(run_id)
    assert run is not None
    assert run.status is not RunStatus.FAILED


@pytest.mark.asyncio
async def test_lease_loss_releases_the_concurrency_slot(sqlite_db) -> None:
    """Stopping early must not leak the slot the run was occupying."""
    run_repo = RunRepository(sqlite_db)
    manager = LeaseManager(sqlite_db, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(sqlite_db, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    assert worker._semaphore._value == 1


@pytest.mark.asyncio
async def test_lease_loss_is_counted_as_a_metric(sqlite_db) -> None:
    """R8: lease loss is distinguishable in metrics, not just in a log line."""
    run_repo = RunRepository(sqlite_db)
    manager = LeaseManager(sqlite_db, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    class _Collector:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        def increment(self, name: str, *args, **kwargs) -> None:
            self.counts[name] = self.counts.get(name, 0) + 1

        def observe(self, name: str, value: float, *args, **kwargs) -> None:
            pass

    collector = _Collector()
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
        metrics_collector=collector,
    )
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(sqlite_db, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    assert collector.counts.get("zeroth_lease_lost_total") == 1
    assert "zeroth_worker_crashes_total" not in collector.counts


@pytest.mark.asyncio
async def test_fencing_rejection_is_counted_as_a_metric(sqlite_db) -> None:
    """R8: a rejected stale write is countable, not only observable as False.

    The counter lives on ``commit_fenced`` rather than on ``LeaseManager``
    itself: the manager's constructor signature is pinned by the immutable
    legacy surface fixture, so the optional collector rides on the new method.
    """

    class _Collector:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        def increment(self, name: str, *args, **kwargs) -> None:
            self.counts[name] = self.counts.get(name, 0) + 1

    collector = _Collector()
    manager = LeaseManager(sqlite_db)
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    await _expire_lease(sqlite_db, run_id)
    await manager.claim_pending(DEPLOYMENT, WORKER_B)

    rejected = await manager.commit_fenced(
        run_id,
        WORKER_A,
        generation=1,
        metrics_collector=collector,
        current_step="stale",
    )
    accepted = await manager.commit_fenced(
        run_id,
        WORKER_B,
        generation=2,
        metrics_collector=collector,
        current_step="live",
    )

    assert rejected is False
    assert accepted is True
    assert collector.counts.get("zeroth_lease_fencing_rejected_total") == 1


@pytest.mark.asyncio
async def test_commit_fenced_refuses_to_write_the_fence_columns(sqlite_db) -> None:
    """A fenced write must not be able to re-grant the lease it is fenced by.

    Without this, `**columns` accepted `lease_worker_id`/`lease_generation`, so a
    displaced worker could hand itself ownership in the very statement the fence
    was meant to reject — making the fence decorative.
    """
    manager = LeaseManager(sqlite_db)
    run_id = await _pending_run(sqlite_db)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    for column, value in (
        ("lease_worker_id", WORKER_A),
        ("lease_generation", 99),
        ("lease_expires_at", "2099-01-01T00:00:00+00:00"),
    ):
        with pytest.raises(ValueError, match="lease columns"):
            await manager.commit_fenced(run_id, WORKER_A, generation=1, **{column: value})

    # The legitimate write still works.
    assert await manager.commit_fenced(
        run_id, WORKER_A, generation=1, current_step="ok"
    ) is True
