from __future__ import annotations

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.runtime.orchestration.token_lifecycle import pause_snapshot, request_cancellation
from zeroth.runtime.orchestration.token_runtime import TokenRuntimeCoordinator
from zeroth.runtime.orchestration.token_scheduler import claim_next_token, initialize_token_snapshot
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.runs import Run, RunStatus


class _MemoryStore:
    def __init__(self, snapshot: TokenEngineSnapshot) -> None:
        self.snapshot = snapshot

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        return self.snapshot if run_id == self.snapshot.run_id else None

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        if expected_revision != self.snapshot.revision:
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        self.snapshot = snapshot
        return snapshot


class _Driver:
    def __init__(self) -> None:
        self.stops = 0

    async def external_stop(self, run: Run) -> Run:
        self.stops += 1
        return run


def _run(run_id: str, status: RunStatus) -> Run:
    return Run(
        run_id=run_id,
        graph_version_ref="graph:v1",
        deployment_ref="deployment",
        status=status,
    )


async def test_paused_snapshot_yields_to_persisted_interrupt_without_claiming() -> None:
    paused = pause_snapshot(
        initialize_token_snapshot(run_id="run-paused", root_node_id="root", payload={})
    )
    store = _MemoryStore(paused)
    driver = _Driver()
    coordinator = TokenRuntimeCoordinator(driver, store)

    result = await coordinator.drive(object(), _run("run-paused", RunStatus.WAITING_INTERRUPT))

    assert result.status is RunStatus.WAITING_INTERRUPT
    assert store.snapshot is paused
    assert driver.stops == 1


async def test_reloaded_cancellation_request_settles_before_returning_failed_run() -> None:
    root = initialize_token_snapshot(run_id="run-cancel", root_node_id="root", payload={})
    cancelling = request_cancellation(claim_next_token(root).snapshot)
    store = _MemoryStore(cancelling)
    driver = _Driver()
    coordinator = TokenRuntimeCoordinator(driver, store)

    result = await coordinator.drive(object(), _run("run-cancel", RunStatus.FAILED))

    assert result.status is RunStatus.FAILED
    assert store.snapshot.state is TokenEngineSnapshotState.CANCELLED
    assert store.snapshot.in_flight_dispatches == ()
    assert driver.stops == 1


async def test_graceful_stop_finalizes_without_claiming_unowned_queue() -> None:
    root = initialize_token_snapshot(run_id="run-stop", root_node_id="root", payload={})
    stopping = root.model_copy(update={"state": TokenEngineSnapshotState.STOPPING})
    store = _MemoryStore(stopping)
    driver = _Driver()
    coordinator = TokenRuntimeCoordinator(driver, store)

    result = await coordinator.drive(object(), _run("run-stop", RunStatus.WAITING_INTERRUPT))

    assert result.status is RunStatus.WAITING_INTERRUPT
    assert store.snapshot.state is TokenEngineSnapshotState.STOPPED
    assert store.snapshot.queue == root.queue
    assert store.snapshot.in_flight_dispatches == ()
    assert driver.stops == 1
