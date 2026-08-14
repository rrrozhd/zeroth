"""ZER-49 follow-ups: a run must never be stranded, and never be stolen.

Three defects, each measured on merged code:

* **F-02** — the ``finally`` in ``_execute_leased_run`` released the *lease* and
  nothing else. On the poll path that is right (``claim_pending`` leaves the run
  PENDING, so the next poll re-claims it). On the **recovery** path the run is
  RUNNING, and ``release_lease`` NULLs ``lease_worker_id``/``lease_expires_at``:
  ``claim_orphaned`` wants ``status=RUNNING AND lease_worker_id IS NOT NULL``,
  ``claim_pending`` wants ``status=PENDING``. A setup throw therefore left the
  run reachable by **no claim path at all** — unrecoverable without operator SQL.
  The tests below assert *reachability*, not a status string.

* **F-07** — ``_install_write_fence`` returns falsey both when there is nothing
  to fence (an unclaimed run: tests and legacy direct drives) and when *another
  worker already holds the lease*. The caller could not tell those apart, so a
  displaced worker started an UNFENCED drive on somebody else's run and its
  ``_mark_failed`` then stamped a terminal status on it.

* **F-10a** — ``graceful_shutdown``'s release loop called ``_stop_token_snapshot``
  unguarded while ``_release_to_pending`` right below it guards itself. One
  ``TokenSnapshotConcurrencyError`` skipped the hand-back for that run *and for
  every run after it in the batch*.
"""

from __future__ import annotations

import asyncio

import pytest

from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import LeaseManager
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.runs import Run, RunStatus

DEPLOYMENT = "zer49-liveness-deployment"
CRASHED_WORKER = "crashed-worker"
OTHER_WORKER = "worker-b"
FRESH_WORKER = "fresh-worker"


class _RecordingOrchestrator:
    """Completes the run and records that it was driven at all."""

    def __init__(self, run_repo: RunRepository) -> None:
        self._run_repo = run_repo
        self.driven: list[str] = []

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        return await self._run_repo.transition(run.run_id, RunStatus.COMPLETED)

    async def resume_graph(self, graph, run_id: str) -> Run | None:
        self.driven.append(run_id)
        return await self._run_repo.get(run_id)

    @property
    def approval_service(self):
        return None


class _RaisingOrchestrator(_RecordingOrchestrator):
    """Fails the drive, so the worker reaches its terminal ``_mark_failed``."""

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        raise RuntimeError("drive exploded")

    async def resume_graph(self, graph, run_id: str) -> Run | None:
        self.driven.append(run_id)
        raise RuntimeError("drive exploded")


class _FakeGraph:
    nodes: list = []
    entry_step: str = "start"


class _GenerationReadFails(LeaseManager):
    """The lease-store read at the top of the setup window throws."""

    async def current_generation(self, run_id: str) -> int | None:
        raise RuntimeError("lease store read failed")


def _worker(
    run_repo: RunRepository,
    lease_manager: LeaseManager,
    orchestrator,
    *,
    max_concurrency: int = 1,
) -> RunWorker:
    return RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=lease_manager,
        max_concurrency=max_concurrency,
    )


async def _expire_lease(db, run_id: str) -> None:
    """Drive the lease into the past so another worker may reclaim it."""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run_id),
        )


async def _recovered_orphan(db, run_repo: RunRepository, worker: RunWorker) -> Run:
    """Seed a RUNNING run abandoned by a dead worker and claim it as an orphan.

    This is the exact state ``_recover_orphans`` dispatches from: RUNNING, held
    by *this* worker, reached through ``claim_orphaned`` rather than
    ``claim_pending``.
    """
    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    assert await worker.lease_manager.claim_pending(DEPLOYMENT, CRASHED_WORKER) == run.run_id
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    await _expire_lease(db, run.run_id)
    assert await worker.lease_manager.claim_orphaned(DEPLOYMENT, worker.worker_id) == [run.run_id]
    return run


async def _reachable_by_any_claim_path(lease_manager: LeaseManager, run_id: str) -> bool:
    """Can *any* claim path still reach this run?

    Deliberately not a status assertion: the defect is that the run falls out
    of both claim predicates at once, and only trying them proves it does not.
    """
    if await lease_manager.claim_pending(DEPLOYMENT, FRESH_WORKER) == run_id:
        return True
    return run_id in await lease_manager.claim_orphaned(DEPLOYMENT, FRESH_WORKER)


# ---------------------------------------------------------------------------
# F-02 -- a setup throw during recovery must not strand the run
# ---------------------------------------------------------------------------


async def test_a_setup_throw_during_recovery_leaves_the_run_claimable(sqlite_db) -> None:
    """The first await of the setup window throws while recovering an orphan."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = _GenerationReadFails(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _recovered_orphan(sqlite_db, run_repo, worker)

    with pytest.raises(RuntimeError, match="lease store read failed"):
        await worker._execute_leased_run(run.run_id, is_recovery=True)

    assert await _reachable_by_any_claim_path(lease_manager, run.run_id), (
        "the recovered run is reachable by no claim path and no sweeper"
    )
    # The liveness fix must not re-open A06-2: the permit still comes back.
    assert worker._semaphore._value == 1


async def test_a_post_fence_setup_throw_during_recovery_leaves_the_run_claimable(
    sqlite_db,
) -> None:
    """The throw lands after the fence is installed and the drive is spawned.

    The worst variant: cleanup has a fence to take down and a live drive task to
    stop before it can hand the run back.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _recovered_orphan(sqlite_db, run_repo, worker)

    # asyncio.create_task(None) raises TypeError, exactly like any renewal-loop
    # construction failure inside the setup window.
    worker._renewal_loop = lambda run_id: None  # type: ignore[assignment]

    with pytest.raises(TypeError):
        await worker._execute_leased_run(run.run_id, is_recovery=True)

    assert await _reachable_by_any_claim_path(lease_manager, run.run_id), (
        "the recovered run is reachable by no claim path and no sweeper"
    )
    assert worker._semaphore._value == 1
    assert worker._active_drives == {}


# ---------------------------------------------------------------------------
# F-07 -- a displaced worker must not start an unfenced drive
# ---------------------------------------------------------------------------


async def test_a_displaced_worker_neither_drives_nor_writes(sqlite_db) -> None:
    """Ownership moved before the drive started: abort, do not run unfenced.

    ``_install_write_fence`` declines here (the holder is no longer us), and the
    caller used to ignore that and drive anyway — with no fence to refuse the
    terminal write the failure handler then issued on the new owner's run.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RaisingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)

    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    assert await lease_manager.claim_pending(DEPLOYMENT, worker.worker_id) == run.run_id
    # Somebody else takes the run over before this worker gets to its setup.
    await _expire_lease(sqlite_db, run.run_id)
    assert await lease_manager.claim_pending(DEPLOYMENT, OTHER_WORKER) == run.run_id

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert orchestrator.driven == [], "a displaced worker drove a run it no longer owns"
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is not RunStatus.FAILED, (
        "a displaced worker wrote a terminal status on the new owner's run"
    )
    # The new owner keeps its lease, and the slot is not leaked.
    assert await lease_manager.current_holder(run.run_id) == OTHER_WORKER
    assert worker._semaphore._value == 1


async def test_an_unclaimed_run_still_drives_unfenced(sqlite_db) -> None:
    """Regression guard for the *other* reason the fence install declines.

    A run nobody holds has nothing to fence against — that is how tests and
    legacy direct drives reach ``_execute_leased_run``. Aborting on it would
    break every one of them, so the abort must key on "another worker holds it",
    never on "no fence was installed".
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)

    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    assert await lease_manager.current_holder(run.run_id) is None

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert orchestrator.driven == [run.run_id]
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# F-10a -- one contended snapshot must not strand the shutdown batch
# ---------------------------------------------------------------------------


class _ContendedSnapshots:
    """A token lifecycle whose durable stop always exhausts its CAS budget."""

    class _Store:
        async def get_token_snapshot(self, run_id: str) -> object:
            del run_id
            return object()  # a snapshot exists, so ``stop`` is reached

    def __init__(self) -> None:
        self.store = self._Store()
        self.attempts: list[str] = []

    async def stop(self, run_id: str) -> None:
        self.attempts.append(run_id)
        # What ``TokenLifecycleAdapter.stop`` re-raises once its bounded CAS
        # retry budget is exhausted.
        raise TokenSnapshotConcurrencyError(run_id, expected_revision=3, actual_revision=4)


async def test_a_contended_snapshot_does_not_strand_the_shutdown_batch() -> None:
    """Every still-running run is handed back, however the snapshot stop ends.

    The failure is injected *below* ``_stop_token_snapshot`` rather than by
    replacing the method, so the guard under test is the production one.
    """
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=object(),  # type: ignore[arg-type]
        orchestrator=object(),  # type: ignore[arg-type]
        graph=_FakeGraph(),  # type: ignore[arg-type]
        lease_manager=object(),  # type: ignore[arg-type]
        max_concurrency=2,
        shutdown_timeout=0.0,
    )
    lifecycle = _ContendedSnapshots()
    worker._token_lifecycle = lifecycle  # type: ignore[assignment]

    released: list[str] = []

    async def _release(run_id: str) -> None:
        released.append(run_id)

    worker._release_to_pending = _release  # type: ignore[method-assign]

    async def _forever() -> None:
        await asyncio.Event().wait()

    for name in ("run-alpha", "run-beta"):
        worker._track(asyncio.create_task(_forever(), name=name))
    await asyncio.sleep(0)

    await worker.graceful_shutdown()

    assert lifecycle.attempts, "the durable stop was never attempted"
    assert sorted(released) == ["alpha", "beta"], (
        f"a contended token snapshot stranded the rest of the batch: released={released}"
    )


# ---------------------------------------------------------------------------
# F-10c -- sustained contention must hand the run back, not fail it
# ---------------------------------------------------------------------------


class _ContendedDriveOrchestrator(_RecordingOrchestrator):
    """The drive exhausts its bounded CAS budget instead of failing the run.

    This is what every ``_claim`` / ``_recover`` / ``_transition`` on the drive
    path raises once eight full-jitter attempts under a 250 ms ceiling are
    spent -- well under one second of total wait.
    """

    async def _drive(self, graph, run) -> Run:
        self.driven.append(run.run_id)
        raise TokenSnapshotConcurrencyError(run.run_id, expected_revision=7, actual_revision=8)

    async def resume_graph(self, graph, run_id: str) -> Run | None:
        self.driven.append(run_id)
        raise TokenSnapshotConcurrencyError(run_id, expected_revision=7, actual_revision=8)


async def test_a_contended_drive_hands_the_run_back_and_converges(sqlite_db) -> None:
    """Sub-second contention must not be a terminal verdict on the run.

    Nothing at the driver or worker boundary caught the exhaustion, so it fell
    through ``except Exception`` into ``_handle_run_exception`` and a
    ``_mark_failed`` write. The claim under test is not merely "not FAILED" but
    *converges*: the run goes back to PENDING and the next claim drives it to
    completion.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    contended = _ContendedDriveOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, contended)

    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    assert await lease_manager.claim_pending(DEPLOYMENT, worker.worker_id) == run.run_id

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert contended.driven == [run.run_id], "the contended drive never ran"
    handed_back = await run_repo.get(run.run_id)
    assert handed_back is not None
    assert handed_back.status is not RunStatus.FAILED, (
        "under a second of contention marked the run terminally FAILED"
    )
    assert worker._semaphore._value == 1

    healthy = _RecordingOrchestrator(run_repo)
    successor = _worker(run_repo, lease_manager, healthy)
    assert await lease_manager.claim_pending(DEPLOYMENT, successor.worker_id) == run.run_id, (
        "the contended run was not left claimable"
    )
    await successor._execute_leased_run(run.run_id, is_recovery=False)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


async def test_a_contended_drive_during_recovery_leaves_the_run_claimable(sqlite_db) -> None:
    """The recovery path, where a bare lease release strands the run outright.

    A recovered orphan is RUNNING with a lease; dropping only the lease matches
    neither ``claim_orphaned`` (wants a non-NULL lease) nor ``claim_pending``
    (wants PENDING). The hand-back is the only disposition that keeps it
    reachable.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    contended = _ContendedDriveOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, contended)
    run = await _recovered_orphan(sqlite_db, run_repo, worker)

    await worker._execute_leased_run(run.run_id, is_recovery=True)

    assert contended.driven == [run.run_id], "the contended drive never ran"
    after = await run_repo.get(run.run_id)
    assert after is not None
    assert after.status is not RunStatus.FAILED, (
        "under a second of contention marked the recovered run terminally FAILED"
    )
    assert worker._semaphore._value == 1
    assert await _reachable_by_any_claim_path(lease_manager, run.run_id), (
        "the contended run is reachable by no claim path and no sweeper"
    )


async def test_an_ordinary_drive_exception_still_fails_the_run(sqlite_db) -> None:
    """The contention branch must be narrow: everything else still terminates.

    ``TokenSnapshotConcurrencyError`` subclasses ``RuntimeError``, so a catch
    written one level too wide would silently convert every worker crash into
    an endless PENDING re-drive.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RaisingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)

    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    assert await lease_manager.claim_pending(DEPLOYMENT, worker.worker_id) == run.run_id

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.FAILED
