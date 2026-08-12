"""Orphan recovery is bounded and task failures are surfaced (ZER-48 / A06-23).

``start()`` iterated the whole orphan list creating one task per element, with no
chunking and no gate — a crash with a large backlog dispatched the entire backlog
at once and ignored ``max_concurrency`` completely.  Separately, ``_track``'s only
done-callback was ``self._active_tasks.discard``: nothing ever called
``task.exception()``, so a task that raised landed nowhere but a GC-time warning.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from zeroth.runtime.orchestration.run_worker import RunWorker


class _Recorder:
    """Counts concurrent drives so a fan-out breach is observable."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.started: list[str] = []
        self.release = asyncio.Event()

    async def drive(self, run_id: str, *, is_recovery: bool, slot_reserved: bool = False) -> None:
        del is_recovery
        self.started.append(run_id)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
            if slot_reserved:
                worker = self.worker  # type: ignore[attr-defined]
                worker._semaphore.release()


def _worker(orphans: list[str], *, max_concurrency: int) -> RunWorker:
    worker = RunWorker.__new__(RunWorker)
    worker.worker_id = "worker-1"
    worker.deployment_ref = "deployment-a"
    worker.max_concurrency = max_concurrency
    worker._semaphore = asyncio.Semaphore(max_concurrency)
    worker._active_tasks = set()
    worker._stopping = False

    class _Leases:
        async def claim_orphaned(self, deployment_ref: str, worker_id: str, **scope: object):
            del deployment_ref, worker_id, scope
            return list(orphans)

    worker.lease_manager = _Leases()  # type: ignore[assignment]
    worker._lease_scope = lambda: {}  # type: ignore[method-assign]
    return worker


@pytest.mark.asyncio
async def test_orphan_recovery_respects_max_concurrency() -> None:
    orphans = [f"run-{i}" for i in range(10)]
    worker = _worker(orphans, max_concurrency=3)
    recorder = _Recorder()
    recorder.worker = worker  # type: ignore[attr-defined]
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)

    assert recorder.peak <= 3, (
        f"orphan recovery ran {recorder.peak} runs at once with max_concurrency=3"
    )
    assert len(recorder.started) <= 3, "the whole backlog was dispatched immediately"

    recorder.release.set()
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_orphan_recovery_eventually_drains_the_backlog() -> None:
    """Bounding the fan-out must not drop the runs beyond the bound."""
    orphans = [f"run-{i}" for i in range(6)]
    worker = _worker(orphans, max_concurrency=2)
    recorder = _Recorder()
    recorder.worker = worker  # type: ignore[attr-defined]
    recorder.release.set()  # every drive completes immediately
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    for _ in range(200):
        if len(recorder.started) == len(orphans):
            break
        await asyncio.sleep(0)

    assert recorder.started == orphans
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_failed_tracked_task_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    worker = _worker([], max_concurrency=1)

    async def _boom() -> None:
        raise RuntimeError("drive exploded outside its own handling")

    logger_name = "zeroth.runtime.orchestration.run_worker"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        task = asyncio.create_task(_boom(), name="drive-run-1")
        worker._track(task)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(5):
            await asyncio.sleep(0)

    messages = [r.getMessage() for r in caplog.records if r.name == logger_name]
    assert any("drive-run-1" in m for m in messages), (
        f"a tracked task's exception was never retrieved or logged: {messages}"
    )


@pytest.mark.asyncio
async def test_a_cancelled_tracked_task_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown cancels drives; that is not an error worth paging on."""
    worker = _worker([], max_concurrency=1)

    async def _forever() -> None:
        await asyncio.Event().wait()

    logger_name = "zeroth.runtime.orchestration.run_worker"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        task = asyncio.create_task(_forever(), name="drive-run-2")
        worker._track(task)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(5):
            await asyncio.sleep(0)

    assert [r for r in caplog.records if r.name == logger_name] == []
