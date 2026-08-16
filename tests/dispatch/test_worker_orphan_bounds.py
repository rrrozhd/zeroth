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
from types import SimpleNamespace

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


def _worker(
    orphans: list[str], *, max_concurrency: int, initially_saturated: bool = False
) -> RunWorker:
    worker = RunWorker.__new__(RunWorker)
    worker.worker_id = "worker-1"
    worker.deployment_ref = "deployment-a"
    worker.max_concurrency = max_concurrency
    worker._semaphore = asyncio.Semaphore(max_concurrency)
    worker._active_tasks = set()
    worker._stopping = False
    worker.poll_interval = 0.01

    class _Leases:
        def __init__(self) -> None:
            self.remaining = list(orphans)
            self.claimed: list[str] = []
            self.claim_limits: list[object] = []
            self.available_slots: list[int] = []
            self.saturated = initially_saturated

        async def claim_orphaned_result(
            self, deployment_ref: str, worker_id: str, **scope: object
        ) -> SimpleNamespace:
            del deployment_ref, worker_id
            self.claim_limits.append(scope.get("claim_limit"))
            self.available_slots.append(worker._semaphore._value)
            limit = scope.pop("claim_limit", None)
            if self.saturated:
                return SimpleNamespace(run_ids=(), concurrency_saturated=True)
            count = len(self.remaining) if limit is None else int(limit)
            claimed = self.remaining[:count]
            del self.remaining[:count]
            self.claimed.extend(claimed)
            return SimpleNamespace(run_ids=tuple(claimed), concurrency_saturated=False)

        async def claim_orphaned(self, deployment_ref: str, worker_id: str, **scope: object):
            result = await self.claim_orphaned_result(deployment_ref, worker_id, **scope)
            return list(result.run_ids)

    worker.lease_manager = _Leases()  # type: ignore[assignment]
    worker._lease_scope = lambda: {}  # type: ignore[method-assign]
    return worker


@pytest.mark.asyncio
async def test_orphans_are_claimed_only_after_reserving_a_local_slot() -> None:
    worker = _worker(["run-1", "run-2"], max_concurrency=1)
    recorder = _Recorder()
    recorder.worker = worker  # type: ignore[attr-defined]
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    for _ in range(20):
        if worker.lease_manager.claim_limits and recorder.started:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0)

    assert worker.lease_manager.claim_limits[0] == 1  # type: ignore[attr-defined]
    assert worker.lease_manager.available_slots[0] == 0  # type: ignore[attr-defined]
    assert recorder.started == ["run-1"]

    recorder.release.set()
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)


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
async def test_saturated_orphan_recovery_waits_and_rescans_after_capacity_frees() -> None:
    worker = _worker(["run-1"], max_concurrency=1, initially_saturated=True)
    recorder = _Recorder()
    recorder.worker = worker  # type: ignore[attr-defined]
    recorder.release.set()
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    for _ in range(50):
        if worker.lease_manager.claim_limits:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0)

    await asyncio.sleep(0)
    assert worker.lease_manager.claim_limits == [1]  # type: ignore[attr-defined]

    worker.lease_manager.saturated = False  # type: ignore[attr-defined]
    for _ in range(100):
        if recorder.started:
            break
        await asyncio.sleep(0.01)

    assert recorder.started == ["run-1"]
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)
    assert worker.lease_manager.claim_limits == [1, 1, 1]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_orphan_recovery_stops_after_a_definitive_empty_scan() -> None:
    worker = _worker([], max_concurrency=1)

    await worker.start()
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)

    assert worker.lease_manager.claim_limits == [1]  # type: ignore[attr-defined]


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


# --- the recovery *loop* is not a run ----------------------------------------


@pytest.mark.asyncio
async def test_the_recovery_loop_task_does_not_decode_to_a_run_id() -> None:
    """Refuse to read the recovery loop's own task name as a run.

    ``_extract_run_id`` strips ``run-``, ``wakeup-`` and ``recover-``, so a loop
    task named ``recover-orphans-<worker>`` parsed as a run called
    ``orphans-<worker>``. The name is read back off the task ``start()``
    actually created rather than restated here, so this tracks the production
    string instead of a copy of it.
    """
    worker = _worker([], max_concurrency=1)
    recorder = _Recorder()
    recorder.worker = worker  # type: ignore[attr-defined]
    recorder.release.set()
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    tasks = list(worker._active_tasks)

    assert len(tasks) == 1, "start() no longer creates exactly one recovery task"
    name = tasks[0].get_name()
    assert worker._extract_run_id(tasks[0]) is None, (
        f"the recovery loop task {name!r} decodes to the run id "
        f"{worker._extract_run_id(tasks[0])!r}, which is not a run"
    )

    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_during_recovery_releases_only_real_orphans() -> None:
    """Release only ids this worker actually claimed, never a fabricated one.

    ``graceful_shutdown`` cancels whatever is still pending and drives
    ``_stop_token_snapshot`` plus ``_release_to_pending`` against the run id it
    reads off each task name. With the loop task inside the parsed namespace
    that drove both against a run that does not exist.
    """
    orphans = [f"run-{index}" for index in range(3)]
    worker = _worker(orphans, max_concurrency=1)
    worker.shutdown_timeout = 0.0
    released: list[str] = []

    async def _release(run_id: str) -> None:
        released.append(run_id)

    async def _no_snapshot(run_id: str) -> None:
        del run_id

    worker._release_to_pending = _release  # type: ignore[method-assign]
    worker._stop_token_snapshot = _no_snapshot  # type: ignore[method-assign]

    recorder = _Recorder()  # drives block, so the loop is still pending at shutdown
    recorder.worker = worker  # type: ignore[attr-defined]
    worker._execute_leased_run = recorder.drive  # type: ignore[method-assign]

    await worker.start()
    for _ in range(20):
        await asyncio.sleep(0)

    await worker.graceful_shutdown()

    fabricated = sorted(set(released) - set(orphans))
    assert not fabricated, f"shutdown released ids that were never orphaned runs: {fabricated}"

    recorder.release.set()
    for task in list(worker._active_tasks):
        task.cancel()
    await asyncio.gather(*list(worker._active_tasks), return_exceptions=True)


# --- a claimed orphan is either started or handed back -----------------------


@pytest.mark.asyncio
async def test_a_stop_mid_recovery_strands_no_claimed_orphan() -> None:
    """Stopping recovery neither strands nor double-releases a claimed orphan."""
    orphans = [f"run-{index}" for index in range(5)]
    worker = _worker(orphans, max_concurrency=1)
    started: list[str] = []
    released: list[str] = []

    async def _drive(run_id: str, *, is_recovery: bool, slot_reserved: bool = False) -> None:
        del is_recovery
        started.append(run_id)
        # Observed by the loop only at its next iteration, which is exactly the
        # mid-loop stop this covers.
        worker._stopping = True
        if slot_reserved:
            worker._semaphore.release()

    async def _release(run_id: str) -> None:
        released.append(run_id)

    worker._execute_leased_run = _drive  # type: ignore[method-assign]
    worker._release_to_pending = _release  # type: ignore[method-assign]

    await worker.start()
    for _ in range(50):
        pending = [task for task in worker._active_tasks if not task.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)

    assert started, "nothing was dispatched, so the mid-loop stop was never exercised"
    claimed = set(worker.lease_manager.claimed)  # type: ignore[attr-defined]
    stranded = sorted(claimed - set(started) - set(released))
    assert not stranded, (
        f"orphans claimed by this worker were neither started nor released: {stranded}"
    )
    assert not set(started) & set(released), "an orphan was both started and released"
