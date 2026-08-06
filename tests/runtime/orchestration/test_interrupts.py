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


async def test_resolving_an_expired_interrupt_raises_interrupt_expired_error() -> None:
    """The expiry path raises its own error and records the expiry.

    ZER-25 found this path broken: ``InterruptExpiredError`` was imported inside
    ``resolve`` from ``zeroth.core.governed.workflows.exceptions``, a module that
    exists nowhere in the repository. Any caller resolving an expired interrupt
    got ``ModuleNotFoundError`` instead, and no test covered it. The error is now
    defined next to the code that raises it, so this pins the behaviour the
    surrounding code always claimed.
    """
    import time

    import pytest

    from zeroth.runtime.orchestration.interrupts import (
        InMemoryInterruptStore,
        InterruptExpiredError,
        InterruptRequest,
    )

    store = InMemoryInterruptStore()
    expired = InterruptRequest(
        interrupt_id="i-1",
        run_id="run-1",
        step_name="approve",
        message="waiting",
        expires_at=int(time.time()) - 60,
    )
    await store.save_request(expired)
    manager = InterruptManager(store=store)

    with pytest.raises(InterruptExpiredError) as caught:
        await manager.resolve(run_id="run-1", interrupt_id="i-1", response={"ok": True})

    # The request travels with the error so a caller can report which interrupt
    # lapsed without a second lookup, and the lapse is persisted rather than
    # left pending for the next poll to trip over again.
    assert caught.value.request is expired
    stored = await store.get_request("run-1", "i-1")
    assert stored is not None
    assert stored.status == "expired"


async def test_listing_pending_interrupts_expires_lapsed_requests() -> None:
    """A lapsed request is never handed out as pending, and the lapse sticks."""
    import time

    from zeroth.runtime.orchestration.interrupts import (
        InMemoryInterruptStore,
        InterruptRequest,
    )

    store = InMemoryInterruptStore()
    now = int(time.time())
    await store.save_request(
        InterruptRequest(
            interrupt_id="lapsed",
            run_id="run-2",
            step_name="approve",
            message="old",
            expires_at=now - 1,
        )
    )
    await store.save_request(
        InterruptRequest(
            interrupt_id="live",
            run_id="run-2",
            step_name="approve",
            message="new",
            expires_at=now + 3600,
        )
    )
    manager = InterruptManager(store=store)

    pending = await manager.list_pending("run-2")

    assert [item.interrupt_id for item in pending] == ["live"]
    lapsed = await store.get_request("run-2", "lapsed")
    assert lapsed is not None
    assert lapsed.status == "expired"
