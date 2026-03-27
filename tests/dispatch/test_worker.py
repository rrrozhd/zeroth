"""Tests for the durable RunWorker."""
from __future__ import annotations

import asyncio

import pytest

from zeroth.dispatch.lease import LeaseManager
from zeroth.dispatch.worker import RunWorker
from zeroth.runs import RunRepository, RunStatus
from zeroth.runs.models import Run
from zeroth.storage import SQLiteDatabase

DEPLOYMENT = "worker-test-deployment"


def _make_run(run_repo: RunRepository) -> Run:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    return run_repo.create(run)


class _FakeOrchestrator:
    """Minimal orchestrator that completes a run synchronously."""

    def __init__(self, run_repo: RunRepository, *, fail: bool = False) -> None:
        self._run_repo = run_repo
        self.fail = fail
        self.driven: list[str] = []

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        if self.fail:
            raise RuntimeError("orchestrator failure")
        run = self._run_repo.transition(run.run_id, RunStatus.COMPLETED)
        return run

    async def resume_graph(self, graph, run_id: str) -> Run:
        run = self._run_repo.get(run_id)
        if run:
            self.driven.append(run_id)
            run = self._run_repo.transition(run_id, RunStatus.COMPLETED)
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
    run_id = worker.lease_manager.claim_pending(worker.deployment_ref, worker.worker_id)
    if run_id is not None:
        task = asyncio.create_task(
            worker._execute_leased_run(run_id, is_recovery=False),
            name=f"run-{run_id}",
        )
        worker._track(task)
        await task


@pytest.mark.asyncio
async def test_worker_drives_pending_run_to_completed(sqlite_db: SQLiteDatabase) -> None:
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

    run = _make_run(run_repo)
    await _worker_tick(worker)

    final = run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
    assert run.run_id in orchestrator.driven


@pytest.mark.asyncio
async def test_worker_respects_concurrency_semaphore(sqlite_db: SQLiteDatabase) -> None:
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

    _make_run(run_repo)
    _make_run(run_repo)

    # Drain both runs.
    for _ in range(2):
        await _worker_tick(worker)

    all_runs = run_repo.list_runs(DEPLOYMENT)
    assert all(r.status is RunStatus.COMPLETED for r in all_runs)


@pytest.mark.asyncio
async def test_worker_marks_failed_on_orchestrator_exception(
    sqlite_db: SQLiteDatabase,
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

    run = _make_run(run_repo)
    await _worker_tick(worker)

    final = run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_worker_recovers_orphaned_run(sqlite_db: SQLiteDatabase) -> None:
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

    run = _make_run(run_repo)
    # Simulate an orphaned RUNNING run with an expired lease.
    run_repo.transition(run.run_id, RunStatus.RUNNING)
    with sqlite_db.transaction() as conn:
        conn.execute(
            """UPDATE runs
               SET lease_worker_id = 'old-worker',
                   lease_expires_at = '2000-01-01T00:00:00+00:00'
               WHERE run_id = ?""",
            (run.run_id,),
        )

    await worker.start()
    # Allow recovery tasks to finish.
    await asyncio.sleep(0.1)

    final = run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_does_not_claim_more_runs_than_available_capacity(
    sqlite_db: SQLiteDatabase,
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

    first_run = _make_run(run_repo)
    second_run = _make_run(run_repo)

    await worker.start()
    poll_task = asyncio.create_task(worker.poll_loop())
    await asyncio.wait_for(orchestrator.started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    with sqlite_db.transaction() as connection:
        row = connection.execute(
            "SELECT lease_worker_id FROM runs WHERE run_id = ?",
            (second_run.run_id,),
        ).fetchone()

    orchestrator.release.set()
    await asyncio.sleep(0.05)
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    assert row["lease_worker_id"] is None
    final = run_repo.get(first_run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
