from __future__ import annotations

import pytest

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import (
    DispatchLifecycleState,
    ForkObligationOutcome,
    SchedulingState,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_lifecycle import (
    TokenLifecycleAdapter,
    acknowledge_cancellation,
    pause_snapshot,
    request_cancellation,
    resume_snapshot,
    stop_snapshot,
)
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    TokenSchedulerTransitionError,
    claim_next_token,
    complete_dispatch,
    fan_out_dispatch,
    initialize_token_snapshot,
)
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError


def _root() -> TokenEngineSnapshot:
    return initialize_token_snapshot(run_id="run-lifecycle", root_node_id="root", payload={"v": 1})


def _fork_with_one_executing_child() -> tuple[TokenEngineSnapshot, object]:
    parent = claim_next_token(_root())
    forked = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="edge-left", payload={"side": "left"}),
            FanOutBranch(node_id="right", inbound_edge_id="edge-right", payload={"side": "right"}),
        ),
    )
    claimed = claim_next_token(forked)
    return claimed.snapshot, claimed.dispatch


def _fork_with_two_executing_children() -> tuple[TokenEngineSnapshot, object, object]:
    snapshot, first = _fork_with_one_executing_child()
    second = claim_next_token(snapshot)
    return second.snapshot, first, second.dispatch


def test_pause_and_resume_preserve_the_complete_snapshot() -> None:
    root = _root()

    paused = pause_snapshot(root)
    resumed = resume_snapshot(paused)

    assert paused.state is TokenEngineSnapshotState.PAUSED
    assert resumed.state is TokenEngineSnapshotState.RUNNING
    assert resumed.queue == root.queue
    assert resumed.tokens == root.tokens
    assert resumed.next_token_ordinal == root.next_token_ordinal


def test_graceful_stop_is_replayable_and_resume_recovers_it() -> None:
    snapshot, dispatch = _fork_with_one_executing_child()

    stopping = stop_snapshot(snapshot)
    replayed = TokenEngineSnapshot.model_validate_json(stopping.model_dump_json())
    first_settled = complete_dispatch(
        replayed,
        dispatch_id=dispatch.dispatch_id,
        attempt=dispatch.attempt,
        cancellation_generation=dispatch.cancellation_generation,
    )
    second = claim_next_token(first_settled)
    drained = complete_dispatch(
        second.snapshot,
        dispatch_id=second.dispatch.dispatch_id,
        attempt=second.dispatch.attempt,
        cancellation_generation=second.dispatch.cancellation_generation,
    )
    stopped = stop_snapshot(drained)
    resumed = resume_snapshot(stopped)

    assert stopping.state is TokenEngineSnapshotState.STOPPING
    assert stopped.state is TokenEngineSnapshotState.STOPPED
    assert stopped.in_flight_dispatches == ()
    assert stopped.queue == ()
    assert resumed.state is TokenEngineSnapshotState.RUNNING


def test_graceful_stop_never_starts_unowned_top_level_work() -> None:
    stopped = stop_snapshot(_root())

    assert stopped.state is TokenEngineSnapshotState.STOPPED
    with pytest.raises(TokenSchedulerTransitionError, match="RUNNING|structured"):
        claim_next_token(stopped)


def test_cancel_settles_queued_children_and_requests_executing_child_stop() -> None:
    snapshot, dispatch = _fork_with_one_executing_child()
    queued_id = snapshot.queue[0].token_id

    cancelling = request_cancellation(snapshot)

    assert cancelling.state is TokenEngineSnapshotState.RUNNING
    assert cancelling.queue == ()
    assert cancelling.cancellation_fence is not None
    assert cancelling.cancellation_fence.generation == 1
    executing = cancelling.in_flight_dispatches[0]
    assert executing.lifecycle_state is DispatchLifecycleState.CANCELLATION_REQUESTED
    assert executing.cancellation_requested_generation == 1
    queued = next(token for token in cancelling.tokens if token.token_id == queued_id)
    assert queued.lifecycle_state is TokenLifecycleState.SETTLED
    assert queued.scheduling_state is SchedulingState.SETTLED
    obligation = next(
        item for item in cancelling.forks[0].obligations if item.child_token_id == queued_id
    )
    assert obligation.outcome is ForkObligationOutcome.CANCELLED

    with pytest.raises(TokenSchedulerTransitionError, match="stale|not in flight"):
        complete_dispatch(
            cancelling,
            dispatch_id=dispatch.dispatch_id,
            attempt=dispatch.attempt,
            cancellation_generation=dispatch.cancellation_generation,
        )


def test_cancel_acknowledgement_compacts_to_replayable_terminal_snapshot() -> None:
    snapshot, dispatch = _fork_with_one_executing_child()
    cancelling = request_cancellation(snapshot)

    cancelled = acknowledge_cancellation(
        cancelling,
        dispatch_id=dispatch.dispatch_id,
        cancellation_generation=1,
    )
    replayed = TokenEngineSnapshot.model_validate_json(cancelled.model_dump_json())

    assert replayed.state is TokenEngineSnapshotState.CANCELLED
    assert replayed.queue == ()
    assert replayed.tokens == ()
    assert replayed.forks == ()
    assert replayed.in_flight_dispatches == ()
    assert replayed.cancellation_fence is not None
    assert replayed.cancellation_fence.acknowledged_token_ids == (dispatch.token.token_id,)

    repeated = acknowledge_cancellation(
        replayed,
        dispatch_id=dispatch.dispatch_id,
        cancellation_generation=1,
    )
    assert repeated is replayed


def test_partial_cancellation_acknowledgement_replays_by_dispatch_identity() -> None:
    snapshot, first, second = _fork_with_two_executing_children()
    cancelling = request_cancellation(snapshot)

    partial = acknowledge_cancellation(
        cancelling,
        dispatch_id=first.dispatch_id,
        cancellation_generation=1,
    )
    restored = TokenEngineSnapshot.model_validate_json(partial.model_dump_json())
    replayed = acknowledge_cancellation(
        restored,
        dispatch_id=first.dispatch_id,
        cancellation_generation=1,
    )

    assert replayed is restored
    assert tuple(item.dispatch_id for item in replayed.in_flight_dispatches) == (
        second.dispatch_id,
    )


def test_cancel_without_executing_children_becomes_terminal_immediately() -> None:
    cancelled = request_cancellation(_root())

    assert cancelled.state is TokenEngineSnapshotState.CANCELLED
    assert cancelled.tokens == ()
    assert cancelled.cancellation_fence is not None
    assert cancelled.cancellation_fence.generation == 1


class _MemoryStore:
    def __init__(self, snapshot: TokenEngineSnapshot) -> None:
        self.snapshot = snapshot
        self.conflicts = 1

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        assert run_id == self.snapshot.run_id
        return self.snapshot

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        if self.conflicts:
            self.conflicts -= 1
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        assert expected_revision == self.snapshot.revision
        self.snapshot = snapshot
        return snapshot


async def test_adapter_retries_cas_and_exposes_idempotent_pause() -> None:
    store = _MemoryStore(_root())
    adapter = TokenLifecycleAdapter(store)

    paused = await adapter.pause("run-lifecycle")
    replay = await adapter.pause("run-lifecycle")

    assert paused.state is TokenEngineSnapshotState.PAUSED
    assert replay is paused
    assert store.snapshot is paused
