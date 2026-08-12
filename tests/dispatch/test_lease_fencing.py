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


@requires_docker
@pytest.mark.asyncio
async def test_lease_loss_cancels_the_running_execution(dual_database) -> None:
    """Ownership loss must stop the work, not merely be logged.

    Before this, ``_renewal_loop`` observed the loss, logged a warning and
    returned -- leaving the displaced worker driving the run to completion
    alongside its new owner.
    """
    run_repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)

    # Another worker takes over while this one is mid-flight.
    await _expire_lease(dual_database, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]

    await asyncio.wait_for(task, timeout=10)

    assert orchestrator.cancelled is True
    assert orchestrator.completed is False


@requires_docker
@pytest.mark.asyncio
async def test_lease_loss_does_not_mark_the_run_failed(dual_database) -> None:
    """The displaced worker must not write a verdict on the new owner's run.

    Marking FAILED here would be a stale write with real consequences: the run
    is not failed, it simply belongs to somebody else now.
    """
    run_repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(dual_database, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    run = await run_repo.get(run_id)
    assert run is not None
    assert run.status is not RunStatus.FAILED


@requires_docker
@pytest.mark.asyncio
async def test_lease_loss_releases_the_concurrency_slot(dual_database) -> None:
    """Stopping early must not leak the slot the run was occupying."""
    run_repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(dual_database, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    assert worker._semaphore._value == 1


@requires_docker
@pytest.mark.asyncio
async def test_lease_loss_is_counted_as_a_metric(dual_database) -> None:
    """R8: lease loss is distinguishable in metrics, not just in a log line."""
    run_repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=2)
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
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await _expire_lease(dual_database, run_id)
    # The run is RUNNING by now, so the realistic takeover path is the orphan
    # reclaim a fresh worker performs at startup -- not claim_pending.
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    await asyncio.wait_for(task, timeout=10)

    assert collector.counts.get("zeroth_lease_lost_total") == 1
    assert "zeroth_worker_crashes_total" not in collector.counts


@pytest.mark.asyncio
async def test_fencing_rejection_is_counted_as_a_metric(dual_database) -> None:
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
    manager = LeaseManager(dual_database)
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    await _expire_lease(dual_database, run_id)
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
async def test_commit_fenced_refuses_to_write_the_fence_columns(dual_database) -> None:
    """A fenced write must not be able to re-grant the lease it is fenced by.

    Each case below defeats the *previous* implementation, which intersected
    exact lowercase names and then interpolated them into SQL: uppercase and
    quoted spellings missed the denylist entirely, and a fragment reached the
    statement text. The allowlist rejects all of them by construction.
    """
    manager = LeaseManager(dual_database)
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)

    bypasses = [
        "lease_worker_id",  # the exact name the old denylist caught
        "LEASE_WORKER_ID",  # case bypass: old check was exact-lowercase
        "lease_generation",
        "Lease_Generation",
        '"lease_worker_id"',  # quoted alias
        "current_step = 'x', lease_generation",  # injected fragment
        "nonexistent_column",
    ]
    for column in bypasses:
        with pytest.raises(ValueError):
            await manager.commit_fenced(run_id, WORKER_A, generation=1, **{column: "x"})

    # None of the rejected attempts may have altered ownership or the fence.
    assert await manager.current_generation(run_id) == 1
    async with dual_database.transaction() as conn:
        row = await conn.fetch_one(
            "SELECT lease_worker_id, current_step FROM runs WHERE run_id = ?", (run_id,)
        )
    assert row["lease_worker_id"] == WORKER_A
    assert row["current_step"] != "x"

    # The legitimate write still works.
    assert await manager.commit_fenced(run_id, WORKER_A, generation=1, current_step="ok") is True


# ---------------------------------------------------------------------------
# ZER26-AUD-004 / AUD-008 -- production writes are fenced, and the events are
# durable evidence rather than log lines
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_a_displaced_workers_run_state_write_is_fenced_out(dual_database) -> None:
    """The fence lives inside the save statement, not in a check that races it.

    With a fence installed, the runs-row upsert carries the lease predicate.
    After a takeover the displaced worker's save returns no row and raises;
    the new owner's row is untouched by the refused write.
    """
    from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError

    repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database)
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    generation = await manager.current_generation(run_id)

    repo.install_fence(run_id, WORKER_A, generation)
    try:
        run = await repo.get(run_id)

        # Still the owner: the fenced save lands.
        run.metadata["written_by"] = WORKER_A
        await repo.put(run)
        assert (await repo.get(run_id)).metadata["written_by"] == WORKER_A

        # Takeover: the run is still PENDING, so expiry plus a fresh
        # claim_pending is the realistic transfer; it advances the generation.
        await _expire_lease(dual_database, run_id)
        assert await manager.claim_pending(DEPLOYMENT, WORKER_B) == run_id

        run.metadata["written_by"] = "stale-worker-a"
        with pytest.raises(FencedRunWriteRejectedError):
            await repo.put(run)
    finally:
        repo.clear_fence(run_id)

    persisted = await repo.get(run_id)
    assert persisted.metadata.get("written_by") == WORKER_A, (
        "the refused write must not reach the new owner's row"
    )


class _FencedWriteOrchestrator:
    """Waits for the test to signal a takeover, then writes run state."""

    def __init__(self, repo: RunRepository) -> None:
        self.repo = repo
        self.started = asyncio.Event()
        self.takeover_done = asyncio.Event()

    async def _drive(self, graph, run):
        self.started.set()
        await asyncio.wait_for(self.takeover_done.wait(), timeout=10)
        run.metadata["written_by"] = "displaced"
        await self.repo.put(run)
        return run

    async def resume_graph(self, graph, run_id: str):
        return None

    @property
    def approval_service(self):
        return None


@requires_docker
@pytest.mark.asyncio
async def test_a_fencing_rejection_leaves_a_durable_audit_record(dual_database) -> None:
    """AUD-008: a refused write must leave evidence, not just a counter.

    The worker wires the fence around the drive; when the fence fires, a
    durable audit record says why this worker stopped. Nothing durable said so
    before -- a displaced worker was indistinguishable from a crashed one.
    """
    from zeroth.governance.audit import AuditRepository

    repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=60)
    orchestrator = _FencedWriteOrchestrator(repo)
    orchestrator.audit_repository = AuditRepository.for_default_compatibility(dual_database)

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)

    await _expire_lease(dual_database, run_id)
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]
    orchestrator.takeover_done.set()

    await asyncio.wait_for(task, timeout=10)

    records = await orchestrator.audit_repository.list_by_run(run_id)
    worker_records = [r for r in records if r.node_id == "__worker__"]
    assert worker_records, "the fencing rejection must be durably recorded"
    assert worker_records[0].execution_metadata["reason_code"] == "lease_fencing_rejected"
    assert worker_records[0].execution_metadata["worker_id"] == worker.worker_id
    persisted = await repo.get(run_id)
    assert persisted.metadata.get("written_by") != "displaced"


@requires_docker
@pytest.mark.asyncio
async def test_a_lease_loss_leaves_a_durable_audit_record(dual_database) -> None:
    """AUD-008: losing the lease is durably recorded by the losing worker."""
    from zeroth.governance.audit import AuditRepository

    repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=2)
    orchestrator = _StallingOrchestrator()
    orchestrator.audit_repository = AuditRepository.for_default_compatibility(dual_database)

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
    )
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, worker.worker_id)

    task = asyncio.create_task(worker._execute_leased_run(run_id, is_recovery=False))
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)

    await _expire_lease(dual_database, run_id)
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B) == [run_id]

    await asyncio.wait_for(task, timeout=10)

    records = await orchestrator.audit_repository.list_by_run(run_id)
    worker_records = [r for r in records if r.node_id == "__worker__"]
    assert worker_records, "the lease loss must be durably recorded"
    assert worker_records[0].execution_metadata["reason_code"] == "lease_lost"


@requires_docker
@pytest.mark.asyncio
async def test_a_displaced_put_leaves_no_checkpoint_or_thread_write(dual_database) -> None:
    """The fence must roll back ALL of put's writes, not just the runs row.

    put_run writes the thread and the checkpoint before the runs row; in
    separate transactions a displaced worker overwrote the durable checkpoint
    state before the fenced save raised. Under a fence all three writes share
    one transaction, so the rejection leaves the checkpoint exactly as the new
    owner needs it.
    """
    from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError

    repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database)
    run_id = await _pending_run(dual_database)
    await manager.claim_pending(DEPLOYMENT, WORKER_A)
    generation = await manager.current_generation(run_id)

    repo.install_fence(run_id, WORKER_A, generation)
    try:
        run = await repo.get(run_id)
        run.metadata["written_by"] = WORKER_A
        await repo.put(run)
        checkpoint_id = run.checkpoint_id
        assert checkpoint_id is not None
        before = await repo.get_checkpoint(checkpoint_id)
        assert before.metadata.get("written_by") == WORKER_A

        # Takeover, then the displaced worker's save must not touch anything.
        await _expire_lease(dual_database, run_id)
        assert await manager.claim_pending(DEPLOYMENT, WORKER_B) == run_id

        run.metadata["written_by"] = "displaced"
        with pytest.raises(FencedRunWriteRejectedError):
            await repo.put(run)
    finally:
        repo.clear_fence(run_id)

    after = await repo.get_checkpoint(checkpoint_id)
    assert after.metadata.get("written_by") == WORKER_A, (
        "a fenced-out put must not overwrite the checkpoint"
    )


@requires_docker
@pytest.mark.asyncio
async def test_graceful_shutdown_stops_the_drive_before_releasing(dual_database) -> None:
    """The voluntary release must not run while the drive task is still alive.

    Releasing first cleared the fence and the lease with the drive still
    executing — an unfenced displaced-writer window. The release now runs only
    after the drive task has been cancelled and awaited.
    """
    repo = RunRepository(dual_database)
    manager = LeaseManager(dual_database, lease_duration_seconds=60)
    orchestrator = _StallingOrchestrator()

    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=manager,
        max_concurrency=1,
        # Short enough that the stalling drive is still alive when the
        # shutdown reaches its release loop — with the default 30s the drive's
        # own sleep expires first and the proof stops discriminating.
        shutdown_timeout=0.5,
    )
    run_id = await _pending_run(dual_database)

    drive_states_at_release: list[bool] = []
    original_release = worker._release_to_pending

    async def _spying_release(target_run_id: str) -> None:
        drive = worker._active_drives.get(target_run_id)
        drive_states_at_release.append(drive is None or drive.done())
        await original_release(target_run_id)

    worker._release_to_pending = _spying_release  # type: ignore[method-assign]

    await worker.start()
    poll_task = asyncio.create_task(worker.poll_loop())
    await asyncio.wait_for(orchestrator.started.wait(), timeout=5)
    await worker.graceful_shutdown()
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    assert drive_states_at_release, "shutdown must reach the voluntary release"
    assert all(drive_states_at_release), (
        "the release must only run after the drive task is finished"
    )
    final = await repo.get(run_id)
    assert final is not None and final.status is RunStatus.PENDING
