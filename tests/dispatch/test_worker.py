"""Tests for the durable RunWorker."""

from __future__ import annotations

import asyncio

import pytest

from zeroth.core.dispatch.lease import LeaseManager
from zeroth.core.dispatch.worker import RunWorker
from zeroth.core.runs import RunRepository, RunStatus
from zeroth.core.runs.models import Run

DEPLOYMENT = "worker-test-deployment"


async def _make_run(run_repo: RunRepository) -> Run:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    return await run_repo.create(run)


class _FakeOrchestrator:
    """Minimal orchestrator that completes a run."""

    def __init__(self, run_repo: RunRepository, *, fail: bool = False) -> None:
        self._run_repo = run_repo
        self.fail = fail
        self.driven: list[str] = []

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        if self.fail:
            raise RuntimeError("orchestrator failure")
        run = await self._run_repo.transition(run.run_id, RunStatus.COMPLETED)
        return run

    async def resume_graph(self, graph, run_id: str) -> Run:
        run = await self._run_repo.get(run_id)
        if run:
            self.driven.append(run_id)
            run = await self._run_repo.transition(run_id, RunStatus.COMPLETED)
        return run

    @property
    def approval_service(self):
        return None


class _BlockingOrchestrator(_FakeOrchestrator):
    def __init__(self, run_repo: RunRepository) -> None:
        super().__init__(run_repo)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        self.started.set()
        await self.release.wait()
        return await super()._drive(graph, run)


class _FakeGraph:
    nodes: list = []
    entry_step: str = "start"


async def _worker_tick(worker: RunWorker) -> None:
    """Run one poll cycle then stop."""
    run_id = await worker.lease_manager.claim_pending(worker.deployment_ref, worker.worker_id)
    if run_id is not None:
        task = asyncio.create_task(
            worker._execute_leased_run(run_id, is_recovery=False),
            name=f"run-{run_id}",
        )
        worker._track(task)
        await task


async def test_worker_drives_pending_run_to_completed(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=4,
    )

    run = await _make_run(run_repo)
    await _worker_tick(worker)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
    assert run.run_id in orchestrator.driven


async def test_worker_respects_concurrency_semaphore(sqlite_db) -> None:
    """With max_concurrency=1, the second run should wait for the first."""
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=1,
    )

    await _make_run(run_repo)
    await _make_run(run_repo)

    # Drain both runs.
    for _ in range(2):
        await _worker_tick(worker)

    all_runs = await run_repo.list_runs(DEPLOYMENT)
    assert all(r.status is RunStatus.COMPLETED for r in all_runs)


async def test_worker_marks_failed_on_orchestrator_exception(
    sqlite_db,
) -> None:
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo, fail=True)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
    )

    run = await _make_run(run_repo)
    await _worker_tick(worker)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.FAILED


async def test_worker_recovers_orphaned_run(sqlite_db) -> None:
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
    )

    run = await _make_run(run_repo)
    # Simulate an orphaned RUNNING run with an expired lease.
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    async with sqlite_db.transaction() as conn:
        await conn.execute(
            """UPDATE runs
               SET lease_worker_id = 'old-worker',
                   lease_expires_at = '2000-01-01T00:00:00+00:00'
               WHERE run_id = ?""",
            (run.run_id,),
        )

    await worker.start()
    # Allow recovery tasks to finish.
    await asyncio.sleep(0.1)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


async def test_worker_does_not_claim_more_runs_than_available_capacity(
    sqlite_db,
) -> None:
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _BlockingOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=1,
        poll_interval=0.01,
    )

    first_run = await _make_run(run_repo)
    second_run = await _make_run(run_repo)

    await worker.start()
    poll_task = asyncio.create_task(worker.poll_loop())
    await asyncio.wait_for(orchestrator.started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT lease_worker_id FROM runs WHERE run_id = ?",
            (second_run.run_id,),
        )

    orchestrator.release.set()
    await asyncio.sleep(0.05)
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    assert row["lease_worker_id"] is None
    final = await run_repo.get(first_run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Wakeup handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_wakeup_claims_and_dispatches(sqlite_db: SQLiteDatabase) -> None:
    """handle_wakeup should claim a pending run and dispatch it."""
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=4,
    )

    run = await _make_run(run_repo)
    await worker.handle_wakeup(run.run_id)
    # Allow the spawned task to complete.
    await asyncio.sleep(0.1)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
    assert run.run_id in orchestrator.driven


@pytest.mark.asyncio
async def test_handle_wakeup_releases_semaphore_on_no_work(
    sqlite_db: SQLiteDatabase,
) -> None:
    """When no work is available, handle_wakeup should release the semaphore."""
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=2,
    )

    # No runs created, so claim returns None.
    await worker.handle_wakeup("nonexistent-run")

    # Semaphore should be back to its initial value.
    assert worker._semaphore._value == 2


# ---------------------------------------------------------------------------
# Graceful shutdown tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_active_tasks(
    sqlite_db: SQLiteDatabase,
) -> None:
    """graceful_shutdown should wait for tasks and release leases on timeout."""
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _BlockingOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
        max_concurrency=4,
        shutdown_timeout=0.1,
    )

    run = await _make_run(run_repo)
    # Start the worker and begin processing.
    await worker.start()
    poll_task = asyncio.create_task(worker.poll_loop())
    await asyncio.wait_for(orchestrator.started.wait(), timeout=1)

    # Shutdown while the task is still running.
    await worker.graceful_shutdown()

    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    # The run should have been released back to PENDING.
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.PENDING


# ---------------------------------------------------------------------------
# Extract run_id tests
# ---------------------------------------------------------------------------


def test_extract_run_id_from_task_name() -> None:
    """_extract_run_id should parse run_id from task name prefixes."""
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=None,  # type: ignore[arg-type]
        orchestrator=None,
        graph=None,
        lease_manager=None,  # type: ignore[arg-type]
    )

    loop = asyncio.new_event_loop()
    try:
        async def _noop():
            pass

        task_run = loop.create_task(_noop(), name="run-abc123")
        task_wakeup = loop.create_task(_noop(), name="wakeup-abc123")
        task_recover = loop.create_task(_noop(), name="recover-abc123")
        task_unknown = loop.create_task(_noop(), name="unknown-task")

        assert worker._extract_run_id(task_run) == "abc123"
        assert worker._extract_run_id(task_wakeup) == "abc123"
        assert worker._extract_run_id(task_recover) == "abc123"
        assert worker._extract_run_id(task_unknown) is None
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Stopping flag test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_flag_exits_poll_loop(sqlite_db: SQLiteDatabase) -> None:
    """Setting _stopping = True should make poll_loop exit without claiming."""
    run_repo = RunRepository(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    graph = _FakeGraph()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=lease_manager,
    )

    await _make_run(run_repo)
    worker._stopping = True

    # poll_loop should return immediately without claiming anything.
    await worker.poll_loop()

    assert len(orchestrator.driven) == 0
