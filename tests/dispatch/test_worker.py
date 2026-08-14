"""Tests for the durable RunWorker."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshotState
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.dispatch.lease import LeaseClaimResult, LeaseManager
from zeroth.runtime.orchestration.token_scheduler import initialize_token_snapshot
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import RunStatus
from zeroth.runtime.runs import Run
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage import NullWorkspaceScopeContext

DEPLOYMENT = "worker-test-deployment"


async def _make_run(run_repo: RunRepository) -> Run:
    run = Run(graph_version_ref="g:v1", deployment_ref=DEPLOYMENT)
    return await run_repo.create(run)


async def _wait_for_status(
    run_repo: RunRepository,
    run_id: str,
    expected: RunStatus,
    *,
    timeout: float = 5.0,
) -> Run | None:
    """Poll until the run reaches the expected status or the deadline passes.

    Fixed sleeps are load-sensitive; polling keeps these tests deterministic
    on a busy machine. Returns the last observed run either way so the caller's
    assertion produces a useful failure message.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        run = await run_repo.get(run_id)
        if run is not None and run.status is expected:
            return run
        if loop.time() >= deadline:
            return run
        await asyncio.sleep(0.02)


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


async def test_worker_refreshes_shared_concurrency_and_keeps_static_local_ceiling() -> None:
    policy_repository = SimpleNamespace(
        current=AsyncMock(return_value=object()),
        effective=AsyncMock(
            side_effect=[
                SimpleNamespace(max_concurrency=2),
                SimpleNamespace(max_concurrency=5),
            ]
        ),
    )
    shared_lease_manager = SimpleNamespace(
        claim_pending_result=AsyncMock(
            side_effect=[
                SimpleNamespace(run_id=None, concurrency_saturated=False),
                SimpleNamespace(run_id=None, concurrency_saturated=False),
            ]
        ),
    )
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=None,  # type: ignore[arg-type]
        orchestrator=None,
        graph=_FakeGraph(),
        lease_manager=shared_lease_manager,  # type: ignore[arg-type]
        max_concurrency=8,
    )
    worker.guardrail_policy_repository = policy_repository

    await worker._claim_pending()
    await worker._claim_pending()

    assert [
        call.kwargs["max_concurrency"]
        for call in shared_lease_manager.claim_pending_result.call_args_list
    ] == [2, 5]
    assert worker._semaphore._value == 8

    static_lease_manager = SimpleNamespace(
        claim_pending_result=AsyncMock(
            return_value=SimpleNamespace(run_id=None, concurrency_saturated=False)
        ),
    )
    static_worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=None,  # type: ignore[arg-type]
        orchestrator=None,
        graph=_FakeGraph(),
        lease_manager=static_lease_manager,  # type: ignore[arg-type]
        max_concurrency=3,
    )
    await static_worker._claim_pending()
    assert static_lease_manager.claim_pending_result.call_args.kwargs["max_concurrency"] is None
    assert static_worker._semaphore._value == 3


async def test_interleaved_poll_and_wakeup_keep_saturation_per_claim() -> None:
    first_ready = asyncio.Event()
    second_done = asyncio.Event()

    class _InterleavedClaims:
        def __init__(self) -> None:
            self.calls = 0
            self.last_claim_saturated = False

        async def _result(self):
            self.calls += 1
            if self.calls == 1:
                self.last_claim_saturated = True
                first_ready.set()
                await second_done.wait()
                return LeaseClaimResult(
                    run_id=None,
                    concurrency_saturated=True,
                    active_count=1,
                    max_concurrency=1,
                )
            await first_ready.wait()
            self.last_claim_saturated = False
            second_done.set()
            return LeaseClaimResult(
                run_id="run-from-wakeup",
                concurrency_saturated=False,
                active_count=0,
                max_concurrency=1,
            )

        async def claim_pending(self, *args, **kwargs):
            del args, kwargs
            return (await self._result()).run_id

        async def claim_pending_result(self, *args, **kwargs):
            del args, kwargs
            return await self._result()

    metrics = MetricsCollector()
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=None,  # type: ignore[arg-type]
        orchestrator=None,
        graph=_FakeGraph(),
        lease_manager=_InterleavedClaims(),  # type: ignore[arg-type]
        metrics_collector=metrics,
    )

    poll = asyncio.create_task(worker._claim_pending(), name="poll-claim")
    await first_ready.wait()
    wakeup = asyncio.create_task(worker._claim_pending(), name="wakeup-claim")
    assert await asyncio.gather(poll, wakeup) == [None, "run-from-wakeup"]

    assert (
        metrics.snapshot()["counters"]['zeroth_guardrail_rejections_total{reason="concurrency"}']
        == 1
    )


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


async def test_renewal_task_failure_does_not_leak_semaphore(sqlite_db) -> None:
    # B4: if the lease-renewal task finishes by RAISING a non-CancelledError
    # (e.g. its renew_lease DB transaction hit "database is locked"), the finally
    # block must still release the lease and the concurrency slot. Before the fix
    # the re-raised exception escaped the finally, permanently leaking the slot.
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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

    async def _raising_renewal(run_id: str) -> None:
        raise RuntimeError("database is locked")

    worker._renewal_loop = _raising_renewal  # renewal dies by raising, not cancel

    run = await _make_run(run_repo)
    # Must complete WITHOUT the RuntimeError escaping _execute_leased_run.
    await worker._execute_leased_run(run.run_id, is_recovery=False)

    # The one concurrency slot was released (not leaked).
    assert worker._semaphore._value == 1
    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


async def test_worker_drives_pending_run_to_completed(sqlite_db) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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


async def test_worker_persists_failed_parallel_resume_accounting(sqlite_db) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from zeroth.contracts.graph import (
        AgentNode,
        AgentNodeData,
        Edge,
        ExecutionSettings,
        Graph,
        SubgraphNode,
    )
    from zeroth.integrations.execution import ExecutableUnitRunner
    from zeroth.platform.measurement import MeasurementState
    from zeroth.runtime.orchestration import RuntimeOrchestrator
    from zeroth.runtime.runs import RunFailureState, RunHistoryEntry
    from zeroth.runtime.runs.costs import rollup_run_cost
    from zeroth.runtime.subgraphs import SubgraphExecutor, SubgraphNodeData
    from zeroth.runtime.parallel.models import ParallelConfig

    run_repo = RunRepository(sqlite_db, NullWorkspaceScopeContext.for_default_compatibility())
    source = AgentNode(
        node_id="source",
        graph_version_ref="g:v1",
        agent=AgentNodeData(instruction="x", model_provider="source"),
        parallel_config=ParallelConfig(split_path="items"),
    )
    child_node = SubgraphNode(
        node_id="child-node",
        graph_version_ref="g:v1",
        subgraph=SubgraphNodeData(graph_ref="child-wf"),
    )
    graph = Graph(
        graph_id="g",
        name="g",
        version=1,
        nodes=[source, child_node],
        edges=[Edge(edge_id="e", source_node_id="source", target_node_id="child-node")],
        entry_step="source",
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
    )

    def history(node_id: str, cost: float, audit_ref: str) -> dict:
        return RunHistoryEntry(
            node_id=node_id,
            status="completed",
            audit_ref=audit_ref,
            cost_usd=cost,
            cost_measurement=MeasurementState.MEASURED,
        ).model_dump(mode="json")

    run_id = "worker-parallel-resume"
    run = await run_repo.create(
        Run(
            run_id=run_id,
            graph_version_ref="g:v1",
            deployment_ref=DEPLOYMENT,
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=["source"],
            metadata={
                "approval_resolved_id": "approval-1",
                "node_payloads": {"source": {}},
                "pending_parallel_subgraph": {
                    "node_id": "source",
                    "split_input": {"items": [{"v": 0}, {"v": 1}, {"v": 2}]},
                    "source_input": {},
                    "source_audit": {
                        "cost_usd": 0.1,
                        "cost_measurement": MeasurementState.MEASURED,
                    },
                    "completed_branches": [
                        {
                            "branch_index": 0,
                            "output": {"done": True},
                            "cost_usd": 0.2,
                            "cost_measurement": MeasurementState.MEASURED,
                            "audit_refs": [f"{run_id}:branch:0:audit:1"],
                            "execution_history": [
                                history("completed-sibling", 0.2, f"{run_id}:branch:0:audit:1")
                            ],
                        }
                    ],
                    "paused_branch": {
                        "branch_index": 1,
                        "child_run_id": "child-run-1",
                        "graph_ref": "child-wf",
                        "node_id": "child-node",
                        "branch_context": {
                            "branch_index": 1,
                            "branch_id": f"{run_id}:branch:1",
                            "input_payload": {"v": 1},
                            "audit_refs": [f"{run_id}:branch:1:audit:1"],
                            "execution_history": [
                                history("paused-prior", 0.05, f"{run_id}:branch:1:audit:1")
                            ],
                            "metadata": {"subgraph_input": {"v": 1}},
                        },
                    },
                    "cancelled_branches": [
                        {
                            "branch_index": 2,
                            "branch_id": f"{run_id}:branch:2",
                            "input_payload": {"v": 2},
                            "audit_refs": [f"{run_id}:branch:2:audit:1"],
                            "execution_history": [
                                history(
                                    "cancelled-sibling",
                                    0.15,
                                    f"{run_id}:branch:2:audit:1",
                                )
                            ],
                            "metadata": {"subgraph_input": {"v": 2}},
                        }
                    ],
                },
            },
        )
    )
    failed_child = Run(
        run_id="child-run-1",
        graph_version_ref="child-wf:v1",
        deployment_ref="child-wf",
        status=RunStatus.FAILED,
        failure_state=RunFailureState(reason="child_failed", message="boom"),
        execution_history=[
            RunHistoryEntry(
                node_id="paid-child",
                status="failed",
                cost_usd=0.3,
                cost_measurement=MeasurementState.MEASURED,
            )
        ],
    )
    subgraphs = MagicMock(spec=SubgraphExecutor)
    subgraphs.resume = AsyncMock(return_value=failed_child)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(),
        subgraph_executor=subgraphs,
    )
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=graph,
        lease_manager=LeaseManager(sqlite_db),
    )

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    final = await run_repo.get(run.run_id)
    assert final is not None
    assert final.status is RunStatus.FAILED
    by_node = [(entry.node_id, entry.status, entry.cost_usd) for entry in final.execution_history]
    assert by_node.count(("source", "completed", 0.1)) == 1
    assert by_node.count(("completed-sibling", "completed", 0.2)) == 1
    assert by_node.count(("paused-prior", "completed", 0.05)) == 1
    assert by_node.count(("cancelled-sibling", "completed", 0.15)) == 1
    assert by_node.count(("child-node", "failed", 0.3)) == 1
    assert by_node.count(("child-node", "cancelled", None)) == 1
    cost = rollup_run_cost(final)
    assert cost.cost_usd == pytest.approx(0.8)
    assert cost.cost_measurement is MeasurementState.UNMEASURED


async def test_worker_recovers_orphaned_run(sqlite_db) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    # Wait for recovery tasks to finish.
    final = await _wait_for_status(run_repo, run.run_id, RunStatus.COMPLETED)
    assert final is not None
    assert final.status is RunStatus.COMPLETED


async def test_worker_does_not_claim_more_runs_than_available_capacity(
    sqlite_db,
) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    # Grace period of several poll intervals so a buggy over-claim of the
    # second run would have a chance to show up (a longer wait under load
    # only makes this negative check stricter, never flaky).
    await asyncio.sleep(0.05)

    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT lease_worker_id FROM runs WHERE run_id = ?",
            (second_run.run_id,),
        )

    orchestrator.release.set()
    completed = await _wait_for_status(run_repo, first_run.run_id, RunStatus.COMPLETED)
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    assert row["lease_worker_id"] is None
    assert completed is not None
    assert completed.status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Wakeup handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_wakeup_claims_and_dispatches(sqlite_db: AsyncSQLiteDatabase) -> None:
    """handle_wakeup should claim a pending run and dispatch it."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    # Wait for the spawned task to complete.
    final = await _wait_for_status(run_repo, run.run_id, RunStatus.COMPLETED)
    assert final is not None
    assert final.status is RunStatus.COMPLETED
    assert run.run_id in orchestrator.driven


@pytest.mark.asyncio
async def test_handle_wakeup_releases_semaphore_on_no_work(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """When no work is available, handle_wakeup should release the semaphore."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """graceful_shutdown should wait for tasks and release leases on timeout."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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


@pytest.mark.asyncio
async def test_graceful_shutdown_stops_token_snapshot_before_releasing_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _BlockingOrchestrator(run_repo)
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=lease_manager,
        shutdown_timeout=0.01,
    )
    run = await _make_run(run_repo)
    snapshot = initialize_token_snapshot(run_id=run.run_id, root_node_id="start", payload={})
    await run_repo.compare_and_swap_token_snapshot(
        run.run_id, expected_revision=None, snapshot=snapshot
    )

    await worker.start()
    poll_task = asyncio.create_task(worker.poll_loop())
    await asyncio.wait_for(orchestrator.started.wait(), timeout=1)
    await worker.graceful_shutdown()
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task

    stopped = await run_repo.get_token_snapshot(run.run_id)
    final = await run_repo.get(run.run_id)
    assert stopped is not None
    assert stopped.state is TokenEngineSnapshotState.STOPPED
    assert final is not None and final.status is RunStatus.PENDING


@pytest.mark.asyncio
async def test_recovery_resumes_stopped_token_snapshot_before_driving(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
    lease_manager = LeaseManager(sqlite_db)
    orchestrator = _FakeOrchestrator(run_repo)
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=run_repo,
        orchestrator=orchestrator,
        graph=_FakeGraph(),
        lease_manager=lease_manager,
    )
    run = await _make_run(run_repo)
    snapshot = initialize_token_snapshot(run_id=run.run_id, root_node_id="start", payload={})
    await run_repo.compare_and_swap_token_snapshot(
        run.run_id,
        expected_revision=None,
        snapshot=snapshot.model_copy(update={"state": TokenEngineSnapshotState.STOPPED}),
    )

    await worker._execute_leased_run(run.run_id, is_recovery=False)

    resumed = await run_repo.get_token_snapshot(run.run_id)
    assert resumed is not None
    assert resumed.state is TokenEngineSnapshotState.RUNNING


# ---------------------------------------------------------------------------
# Extract run_id tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_run_id_from_task_name() -> None:
    """_extract_run_id should parse run_id from task name prefixes."""
    worker = RunWorker(
        deployment_ref=DEPLOYMENT,
        run_repository=None,  # type: ignore[arg-type]
        orchestrator=None,
        graph=None,
        lease_manager=None,  # type: ignore[arg-type]
    )

    async def _noop() -> None:
        pass

    tasks = {
        name: asyncio.create_task(_noop(), name=name)
        for name in ("run-abc123", "wakeup-abc123", "recover-abc123", "unknown-task")
    }
    try:
        assert worker._extract_run_id(tasks["run-abc123"]) == "abc123"
        assert worker._extract_run_id(tasks["wakeup-abc123"]) == "abc123"
        assert worker._extract_run_id(tasks["recover-abc123"]) == "abc123"
        assert worker._extract_run_id(tasks["unknown-task"]) is None
    finally:
        await asyncio.gather(*tasks.values())


# ---------------------------------------------------------------------------
# Stopping flag test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_flag_exits_poll_loop(sqlite_db: AsyncSQLiteDatabase) -> None:
    """Setting _stopping = True should make poll_loop exit without claiming."""
    run_repo = RunRepository.for_default_compatibility(sqlite_db)
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
