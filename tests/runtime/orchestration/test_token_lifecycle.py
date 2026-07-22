from __future__ import annotations

import pytest

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import (
    DeferredJoinDelivery,
    DispatchLifecycleState,
    ForkObligationOutcome,
    IterationFrameState,
    IterationMemberState,
    JoinLifecycleState,
    JoinObligationOutcome,
    LoopLifecycleState,
    PayloadDelivery,
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
from zeroth.runtime.orchestration.token_joins import deliver_to_join
from zeroth.runtime.orchestration.token_loops import enter_loop, settle_loop_member
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


def _nested_loop_fanout(*, failure_mode: str) -> TokenEngineSnapshot:
    root = initialize_token_snapshot(
        run_id=f"run-nested-{failure_mode}",
        root_node_id="outer",
        payload={},
        failure_mode=failure_mode,
    )
    outer = enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="outer",
        body_node_id="inner",
        inbound_edge_id="outer-inner",
        exit_routes={"outer-exit": "done"},
    )
    inner_owner = outer.queue[0]
    inner = enter_loop(
        outer,
        token_id=inner_owner.token_id,
        loop_header_node_id="inner",
        body_node_id="body",
        inbound_edge_id="inner-body",
        exit_routes={"inner-exit": "outer-tail"},
    )
    claim = claim_next_token(inner)
    return fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=claim.dispatch.attempt,
        cancellation_generation=claim.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="body-left", payload="left"),
            FanOutBranch(node_id="right", inbound_edge_id="body-right", payload="right"),
        ),
    )


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


def test_graceful_stop_from_pause_waits_for_barrier_ready_loop_owner() -> None:
    root = _root()
    entered = enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="root",
        body_node_id="body",
        inbound_edge_id="root-body",
        exit_routes={"body-exit": "done"},
    )
    member = entered.queue[0]
    ready = settle_loop_member(
        entered,
        token_id=member.token_id,
        outcome=IterationMemberState.INTERNAL_COMPLETION,
    )

    stopping = stop_snapshot(pause_snapshot(ready))

    assert ready.loops[0].frames[-1].state is IterationFrameState.BARRIER_READY
    assert stopping.state is TokenEngineSnapshotState.STOPPING


def test_graceful_stop_waits_for_ready_join_owner() -> None:
    root = _root()
    parent = claim_next_token(root)
    snapshot = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="root-left", payload="left"),
            FanOutBranch(node_id="right", inbound_edge_id="root-right", payload="right"),
        ),
    )
    routes = {
        child.token_id: f"join-{child.creation_ordinal}"
        for child in snapshot.forks[0].children
    }
    for value in ("left", "right"):
        claim = claim_next_token(snapshot)
        snapshot = deliver_to_join(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=claim.dispatch.attempt,
            cancellation_generation=claim.dispatch.cancellation_generation,
            target_node_id="join",
            inbound_edge_id=routes[claim.dispatch.token.token_id],
            cohort_inbound_edges=routes,
            payload=value,
        )

    stopping = stop_snapshot(snapshot)

    assert snapshot.joins[0].lifecycle_state is JoinLifecycleState.READY
    assert stopping.state is TokenEngineSnapshotState.STOPPING


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


def test_cancel_acknowledgement_retains_replayable_terminal_causal_state() -> None:
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
    assert all(token.scheduling_state is SchedulingState.SETTLED for token in replayed.tokens)
    assert all(fork.outstanding_child_count == 0 for fork in replayed.forks)
    assert replayed.in_flight_dispatches == ()
    assert replayed.cancellation_fence is not None
    assert replayed.cancellation_fence.acknowledged_token_ids == (dispatch.token.token_id,)

    repeated = acknowledge_cancellation(
        replayed,
        dispatch_id=dispatch.dispatch_id,
        cancellation_generation=1,
    )
    assert repeated is replayed
    with pytest.raises(TokenSchedulerTransitionError, match="not in flight|cancellation"):
        complete_dispatch(
            replayed,
            dispatch_id=dispatch.dispatch_id,
            attempt=dispatch.attempt,
            cancellation_generation=dispatch.cancellation_generation,
        )


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
    assert len(cancelled.tokens) == 1
    assert cancelled.tokens[0].scheduling_state is SchedulingState.SETTLED
    assert cancelled.cancellation_fence is not None
    assert cancelled.cancellation_fence.generation == 1


@pytest.mark.parametrize(
    ("failure_mode", "expected"),
    [
        ("fail_fast", IterationMemberState.CANCELLED),
        ("best_effort", IterationMemberState.SUPPRESSED),
    ],
)
def test_nested_cancellation_settles_inner_first_and_propagates_once(
    failure_mode: str,
    expected: IterationMemberState,
) -> None:
    cancelled = request_cancellation(_nested_loop_fanout(failure_mode=failure_mode))

    assert cancelled.state is TokenEngineSnapshotState.CANCELLED
    inner = next(loop for loop in cancelled.loops if loop.loop_header_node_id == "inner")
    outer = next(loop for loop in cancelled.loops if loop.loop_header_node_id == "outer")
    assert inner.lifecycle_state is LoopLifecycleState.CANCELLED
    assert outer.lifecycle_state is LoopLifecycleState.CANCELLED
    assert sum(member.state is expected for member in inner.frames[-1].members) == 2
    assert sum(member.state is expected for member in outer.frames[-1].members) == 1
    assert all(
        obligation.outcome
        is (
            ForkObligationOutcome.CANCELLED
            if failure_mode == "fail_fast"
            else ForkObligationOutcome.SUPPRESSED
        )
        for obligation in cancelled.forks[-1].obligations
    )
    assert TokenEngineSnapshot.model_validate_json(cancelled.model_dump_json()) == cancelled


@pytest.mark.parametrize(
    ("failure_mode", "expected"),
    [
        ("fail_fast", JoinObligationOutcome.CANCELLED),
        ("best_effort", JoinObligationOutcome.SUPPRESSED),
    ],
)
def test_cancellation_policy_preserves_delivered_join_and_settles_only_pending(
    failure_mode: str,
    expected: JoinObligationOutcome,
) -> None:
    root = initialize_token_snapshot(
        run_id=f"run-join-{failure_mode}",
        root_node_id="root",
        payload={},
        failure_mode=failure_mode,
    )
    parent = claim_next_token(root)
    forked = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="root-left", payload="left"),
            FanOutBranch(node_id="right", inbound_edge_id="root-right", payload="right"),
        ),
    )
    routes = {
        child.token_id: f"join-{child.creation_ordinal}" for child in forked.forks[0].children
    }
    first = claim_next_token(forked)
    arrived = deliver_to_join(
        first.snapshot,
        dispatch_id=first.dispatch.dispatch_id,
        attempt=first.dispatch.attempt,
        cancellation_generation=first.dispatch.cancellation_generation,
        target_node_id="join",
        inbound_edge_id=routes[first.dispatch.token.token_id],
        cohort_inbound_edges=routes,
        payload={"delivered": True},
        failure_mode=failure_mode,
    )

    cancelled = request_cancellation(arrived)
    outcomes = tuple(item.outcome for item in cancelled.joins[0].obligations)

    assert cancelled.joins[0].lifecycle_state is JoinLifecycleState.CANCELLED
    assert outcomes == (JoinObligationOutcome.DELIVERED, expected)
    assert cancelled.forks[0].obligations[0].outcome is ForkObligationOutcome.JOINED


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


class _CompletionWinsRaceStore(_MemoryStore):
    def __init__(self, snapshot: TokenEngineSnapshot, dispatch: object) -> None:
        super().__init__(snapshot)
        self.dispatch = dispatch

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        if self.conflicts:
            self.conflicts -= 1
            self.snapshot = complete_dispatch(
                self.snapshot,
                dispatch_id=self.dispatch.dispatch_id,
                attempt=self.dispatch.attempt,
                cancellation_generation=self.dispatch.cancellation_generation,
            )
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        return await super().compare_and_swap_token_snapshot(
            run_id,
            expected_revision=expected_revision,
            snapshot=snapshot,
        )


async def test_adapter_retries_cas_and_exposes_idempotent_pause() -> None:
    store = _MemoryStore(_root())
    adapter = TokenLifecycleAdapter(store)

    paused = await adapter.pause("run-lifecycle")
    replay = await adapter.pause("run-lifecycle")

    assert paused.state is TokenEngineSnapshotState.PAUSED
    assert replay is paused
    assert store.snapshot is paused


async def test_cancellation_cas_race_preserves_a_winning_completion() -> None:
    snapshot, dispatch = _fork_with_one_executing_child()
    store = _CompletionWinsRaceStore(snapshot, dispatch)
    adapter = TokenLifecycleAdapter(store)

    cancelled = await adapter.cancel("run-lifecycle")

    completed = next(token for token in cancelled.tokens if token.token_id == dispatch.token.token_id)
    assert cancelled.state is TokenEngineSnapshotState.CANCELLED
    assert completed.cancellation_generation == dispatch.cancellation_generation
    assert completed.cancellation_acknowledged_generation is None
    assert completed.settled_revision is not None


async def test_cancellation_discards_persisted_deferred_join_deliveries() -> None:
    root = _root()
    snapshot = TokenEngineSnapshot.model_validate(
        {
            **{name: getattr(root, name) for name in type(root).model_fields},
            "deferred_join_deliveries": (
                DeferredJoinDelivery(
                    delivery_id="delivery-1",
                    source_token_id=root.tokens[0].token_id,
                    target_node_id="join",
                    inbound_edge_id="root-join",
                    delivery=PayloadDelivery(payload={"v": 1}),
                    cancellation_generation=0,
                    created_revision=0,
                ),
            ),
        }
    )
    store = _MemoryStore(snapshot)

    cancelled = await TokenLifecycleAdapter(store).cancel("run-lifecycle")

    assert cancelled.state is TokenEngineSnapshotState.CANCELLED
    assert cancelled.deferred_join_deliveries == ()
