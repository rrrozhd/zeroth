"""ZER-49 A06-2: a throw in the pre-``try`` setup window must not leak a permit.

``RunWorker._execute_leased_run`` acquires the concurrency semaphore and *then*
does four awaits/spawns before entering the ``try`` whose ``finally`` releases
it: the generation read, the write-fence install, the drive task spawn, and the
renewal task spawn. A throw anywhere in that window skipped the whole
``finally`` — the permit, the lease, the fence and the bookkeeping all leaked,
and the already-spawned drive task kept executing unsupervised (with no renewal
loop watching its lease).

Each test asserts on remaining semaphore capacity — not on a log line.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import LeaseManager
from zeroth.platform.storage.scoping import ScopeContext
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.runtime.runs import Run, RunStatus

DEPLOYMENT = "zer49-a06-2-deployment"
TENANT = "setup-recovery-tenant"
WORKSPACE = "setup-recovery-workspace"


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
        return await self._run_repo.transition(run_id, RunStatus.COMPLETED)

    @property
    def approval_service(self):
        return None


class _FakeGraph:
    nodes: list = []
    entry_step: str = "start"


class _GenerationReadFails(LeaseManager):
    """The lease-store read at the top of the setup window throws."""

    async def current_generation(self, run_id: str, **_scope: object) -> int | None:
        raise RuntimeError("lease store read failed")


def _worker(
    run_repo: RunRepository,
    lease_manager: LeaseManager,
    orchestrator,
    *,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
) -> RunWorker:
    return RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=lease_manager,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        max_concurrency=1,
    )


async def _claim_one(run_repo: RunRepository, worker: RunWorker) -> Run:
    run = await run_repo.create(Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT))
    claimed = await worker.lease_manager.claim_pending(DEPLOYMENT, worker.worker_id)
    assert claimed == run.run_id
    return run


async def test_a_failed_generation_read_releases_the_permit(sqlite_db) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = _GenerationReadFails(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _claim_one(run_repo, worker)

    with pytest.raises(RuntimeError, match="lease store read failed"):
        await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert worker._semaphore._value == 1
    # The rest of the finally must have run too: the lease is handed back.
    assert await lease_manager.current_holder(run.run_id) is None
    assert worker._active_drives == {}
    assert worker._lease_generations == {}


async def test_a_failed_fence_install_releases_the_permit(sqlite_db) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _claim_one(run_repo, worker)

    async def _fence_explodes(run_id: str, generation: int | None) -> bool:
        raise RuntimeError("fence install failed")

    worker._install_write_fence = _fence_explodes  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="fence install failed"):
        await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert worker._semaphore._value == 1
    assert await lease_manager.current_holder(run.run_id) is None


async def test_a_failed_renewal_spawn_releases_the_permit_and_stops_the_drive(
    sqlite_db,
) -> None:
    """The worst variant: the drive task is already spawned when the throw lands.

    Before the fix the permit leaked *and* the orphaned drive task kept running
    the run with no renewal loop renewing its lease.
    """
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _claim_one(run_repo, worker)

    # asyncio.create_task(None) raises TypeError, exactly like any renewal-loop
    # construction failure inside the setup window.
    worker._renewal_loop = lambda run_id: None  # type: ignore[assignment]

    with pytest.raises(TypeError):
        await worker._execute_leased_run(run.run_id, is_recovery=False)

    assert worker._semaphore._value == 1
    assert worker._active_drives == {}

    # Give any orphaned drive task a generous window to prove it is still alive.
    await asyncio.sleep(0.25)
    assert orchestrator.driven == []
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.PENDING


async def test_the_normal_path_still_completes_and_releases(sqlite_db) -> None:
    """Regression guard: restructuring must not change the healthy path."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _RecordingOrchestrator(run_repo)
    worker = _worker(run_repo, lease_manager, orchestrator)
    run = await _claim_one(run_repo, worker)

    await worker._execute_leased_run(run.run_id, is_recovery=False, slot_reserved=False)

    assert worker._semaphore._value == 1
    assert orchestrator.driven == [run.run_id]
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


@requires_docker
@pytest.mark.security_rc
@pytest.mark.parametrize("failure_point", ("generation", "fence"))
async def test_reclaimed_run_setup_failure_remains_retryable_and_completes(
    dual_database,
    failure_point: str,
) -> None:
    scope = {"tenant_id": TENANT, "workspace_id": WORKSPACE}
    run_repo = RunRepository(
        dual_database,
        ScopeContext(tenant_id=TENANT, workspace_id=WORKSPACE),
    )
    run = await run_repo.create(
        Run(
            run_id=f"reclaimed-{failure_point}",
            graph_version_ref="g:v1",
            deployment_ref=DEPLOYMENT,
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
        )
    )
    await run_repo.transition(run.run_id, RunStatus.RUNNING)
    async with dual_database.transaction() as connection:
        await connection.execute(
            """UPDATE runs
               SET lease_worker_id = ?, lease_generation = 1,
                   lease_acquired_at = ?, lease_expires_at = ?
               WHERE tenant_id = ? AND workspace_scope = ? AND run_id = ?""",
            (
                "crashed-worker",
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:01:00+00:00",
                TENANT,
                f"value:{WORKSPACE}",
                run.run_id,
            ),
        )

    first_manager: LeaseManager
    if failure_point == "generation":
        first_manager = _GenerationReadFails(dual_database)
    else:
        first_manager = LeaseManager(dual_database)
    first_orchestrator = _RecordingOrchestrator(run_repo)
    first_worker = _worker(
        run_repo,
        first_manager,
        first_orchestrator,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
    )
    assert await first_manager.claim_orphaned(
        DEPLOYMENT,
        first_worker.worker_id,
        **scope,
    ) == [run.run_id]
    assert await first_manager.current_holder(run.run_id, **scope) == first_worker.worker_id
    recovery_checkpoint_id = await first_manager.get_recovery_checkpoint_id(run.run_id, **scope)
    assert recovery_checkpoint_id is not None
    if failure_point == "fence":

        async def _fence_explodes(run_id: str, generation: int | None) -> bool:
            raise RuntimeError("fence install failed")

        first_worker._install_write_fence = _fence_explodes  # type: ignore[method-assign]

    message = "lease store read failed" if failure_point == "generation" else "fence install failed"
    with pytest.raises(RuntimeError, match=message):
        await first_worker._execute_leased_run(run.run_id, is_recovery=True)

    after_failure = await run_repo.get(run.run_id)
    assert after_failure is not None
    recovery_manager = LeaseManager(dual_database)
    assert after_failure.status is RunStatus.RUNNING
    assert await recovery_manager.current_holder(run.run_id, **scope) == first_worker.worker_id
    assert (
        await recovery_manager.get_recovery_checkpoint_id(run.run_id, **scope)
        == recovery_checkpoint_id
    )
    if failure_point == "generation":
        async with dual_database.transaction() as connection:
            await connection.execute(
                """UPDATE runs SET lease_expires_at = ?
                   WHERE tenant_id = ? AND workspace_scope = ? AND run_id = ?""",
                (
                    "2000-01-01T00:00:00+00:00",
                    TENANT,
                    f"value:{WORKSPACE}",
                    run.run_id,
                ),
            )

    final_orchestrator = _RecordingOrchestrator(run_repo)
    final_worker = _worker(
        run_repo,
        recovery_manager,
        final_orchestrator,
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
    )
    assert await recovery_manager.claim_orphaned(
        DEPLOYMENT,
        final_worker.worker_id,
        **scope,
    ) == [run.run_id]

    await final_worker._execute_leased_run(run.run_id, is_recovery=True)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
    assert final_orchestrator.driven == [run.run_id]
