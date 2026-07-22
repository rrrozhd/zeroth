from __future__ import annotations

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.runtime.orchestration.interrupts import InterruptManager
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_scheduler import initialize_token_snapshot
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError


class _MemoryStore:
    def __init__(self) -> None:
        self.snapshot = initialize_token_snapshot(
            run_id="run-interrupt", root_node_id="root", payload={"value": 1}
        )

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        return self.snapshot if run_id == self.snapshot.run_id else None

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        if run_id != self.snapshot.run_id or expected_revision != self.snapshot.revision:
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        self.snapshot = snapshot
        return snapshot


async def test_interrupt_manager_routes_token_pause_resume_and_cancel() -> None:
    store = _MemoryStore()
    manager = InterruptManager(token_lifecycle=TokenLifecycleAdapter(store))

    paused = await manager.pause_run("run-interrupt")
    resumed = await manager.resume_run("run-interrupt")
    cancelled = await manager.cancel_run("run-interrupt")

    assert paused.state is TokenEngineSnapshotState.PAUSED
    assert resumed.state is TokenEngineSnapshotState.RUNNING
    assert cancelled.state is TokenEngineSnapshotState.CANCELLED


async def test_interrupt_manager_requires_lifecycle_adapter_for_token_commands() -> None:
    manager = InterruptManager()

    try:
        await manager.pause_run("run-interrupt")
    except RuntimeError as exc:
        assert "TokenLifecycleAdapter" in str(exc)
    else:  # pragma: no cover - assertion spelling
        raise AssertionError("token pause accepted no lifecycle adapter")
