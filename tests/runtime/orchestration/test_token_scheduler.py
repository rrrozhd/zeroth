"""Pure structured-token scheduling transitions and their CAS boundary."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from zeroth.contracts.graph import (
    CancellationFence,
    DispatchLifecycleState,
    ForkLifecycleState,
    ForkObligationOutcome,
    InFlightDispatch,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    IterationMembership,
    JoinLifecycleState,
    JoinObligationOutcome,
    LoopEnclosingOwner,
    LoopInstance,
    LoopLifecycleState,
    ProvenanceFrame,
    SchedulingState,
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
    TokenLifecycleState,
    TokenEnvelope,
)
from zeroth.runtime.orchestration import (
    DispatchClaim,
    FanOutBranch,
    PostCommitEffect,
    TokenPostCommitError,
    TokenSchedulerTransitionError,
    TokenTransition,
    apply_token_transition,
    claim_next_token,
    complete_dispatch,
    deliver_to_join,
    enqueue_dispatch,
    fail_dispatch,
    fan_out_dispatch,
    initialize_token_snapshot,
    recover_dispatch,
    retry_dispatch,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
)
from zeroth.runtime.orchestration.token_lifecycle import request_cancellation


def _root(*, payload: object = None) -> TokenEngineSnapshot:
    return initialize_token_snapshot(
        run_id="run-1",
        root_node_id="entry",
        payload={"nested": [payload, {"preserved": True}]},
    )


def _claim(snapshot: TokenEngineSnapshot | None = None) -> DispatchClaim:
    return claim_next_token(_root() if snapshot is None else snapshot)


def test_initialize_enqueues_exact_deterministic_root() -> None:
    first = _root(payload="one")
    second = _root(payload="one")

    assert first == second
    assert first.revision == 0
    assert first.next_token_ordinal == 1
    assert len(first.tokens) == len(first.queue) == 1
    assert first.tokens[0] == first.queue[0]
    assert first.tokens[0].scheduling_state is SchedulingState.QUEUED
    assert first.tokens[0].lifecycle_state is TokenLifecycleState.ACTIVE
    assert first.tokens[0].token_id.startswith("tok_")


def test_root_allocation_is_run_scoped_and_collision_free() -> None:
    first = initialize_token_snapshot(run_id="run-1", root_node_id="entry", payload=None)
    other = initialize_token_snapshot(run_id="run-2", root_node_id="entry", payload=None)

    assert first.tokens[0].token_id != other.tokens[0].token_id


def test_queue_claim_and_completion_increment_once_per_transition() -> None:
    root = _root()
    claim = claim_next_token(root)

    assert claim.snapshot.revision == root.revision + 1
    assert claim.snapshot.queue == ()
    assert claim.dispatch.token.scheduling_state is SchedulingState.EXECUTING
    assert claim.snapshot.tokens == (claim.dispatch.token,)
    assert claim.snapshot.in_flight_dispatches == (claim.dispatch,)

    settled = complete_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=claim.dispatch.attempt,
        cancellation_generation=claim.dispatch.cancellation_generation,
    )

    assert settled.revision == claim.snapshot.revision + 1
    assert settled.in_flight_dispatches == ()
    assert settled.tokens[0].scheduling_state is SchedulingState.SETTLED
    assert settled.tokens[0].settled_revision == settled.revision


def test_claim_and_retry_preserve_full_payload_and_provenance() -> None:
    root = _root(payload={"deep": [1, None, {"x": "y"}]})
    original = root.tokens[0]

    claim = claim_next_token(root)
    retried = retry_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=claim.dispatch.attempt,
        cancellation_generation=claim.dispatch.cancellation_generation,
    )

    assert retried.dispatch.token.payload == original.payload
    assert retried.dispatch.token.provenance_tag == original.provenance_tag
    assert retried.dispatch.token.fork_lineage == original.fork_lineage
    assert retried.dispatch.token.iteration_memberships == original.iteration_memberships


def test_ordinary_completion_requeues_same_token_at_successor() -> None:
    claim = _claim(_root(payload="input"))

    completed = enqueue_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        next_node_id="successor",
        inbound_edge_id="edge-successor",
        payload={"output": [1, 2]},
    )

    token = completed.tokens[0]
    assert completed.revision == claim.snapshot.revision + 1
    assert completed.queue == (token,)
    assert completed.in_flight_dispatches == ()
    assert token.token_id == claim.dispatch.token.token_id
    assert token.current_node_id == "successor"
    assert token.causal_inbound_edge_id == "edge-successor"
    assert token.model_dump(mode="json")["payload"] == {"output": [1, 2]}
    assert token.provenance_tag == claim.dispatch.token.provenance_tag
    assert token.fork_lineage == claim.dispatch.token.fork_lineage
    assert token.iteration_memberships == claim.dispatch.token.iteration_memberships
    assert token.scheduling_state is SchedulingState.QUEUED


def test_ordinary_completion_resets_retry_attempt_for_next_logical_execution() -> None:
    first = _claim()
    retried = retry_dispatch(
        first.snapshot,
        dispatch_id=first.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )
    assert retried.dispatch.attempt == 1

    queued = enqueue_dispatch(
        retried.snapshot,
        dispatch_id=retried.dispatch.dispatch_id,
        attempt=1,
        cancellation_generation=0,
        next_node_id="successor",
        inbound_edge_id="edge-successor",
        payload={"done": True},
    )
    next_claim = claim_next_token(queued)

    assert queued.tokens[0].retry_attempt == 0
    assert next_claim.dispatch.attempt == 0


@pytest.mark.parametrize("next_node_id", ["entry", "successor"])
def test_requeued_claim_gets_new_dispatch_identity_while_its_retries_stay_stable(
    next_node_id: str,
) -> None:
    first = _claim(_root(payload="input"))
    queued = enqueue_dispatch(
        first.snapshot,
        dispatch_id=first.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        next_node_id=next_node_id,
        inbound_edge_id="edge-successor",
        payload={"output": True},
    )

    second = claim_next_token(queued)
    assert second.dispatch.dispatch_id != first.dispatch.dispatch_id
    assert second.dispatch.idempotency_key != first.dispatch.idempotency_key

    retry = retry_dispatch(
        second.snapshot,
        dispatch_id=second.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )
    assert retry.dispatch.dispatch_id == second.dispatch.dispatch_id
    assert retry.dispatch.idempotency_key == second.dispatch.idempotency_key


def test_retry_increments_attempt_but_keeps_dispatch_and_idempotency_identity() -> None:
    claim = _claim()

    retried = retry_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    assert retried.snapshot.revision == claim.snapshot.revision + 1
    assert retried.dispatch.attempt == 1
    assert retried.dispatch.token.retry_attempt == 1
    assert retried.dispatch.dispatch_id == claim.dispatch.dispatch_id
    assert retried.dispatch.idempotency_key == claim.dispatch.idempotency_key


def test_crash_recovery_creates_new_attempt_without_queue_duplication() -> None:
    claim = _claim()

    recovered = recover_dispatch(claim.snapshot, dispatch_id=claim.dispatch.dispatch_id)

    assert recovered.dispatch.attempt == 1
    assert recovered.snapshot.queue == ()
    assert len(recovered.snapshot.tokens) == 1
    assert len(recovered.snapshot.in_flight_dispatches) == 1


def test_duplicate_or_out_of_order_completion_is_rejected() -> None:
    claim = _claim()
    settled = complete_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    with pytest.raises(TokenSchedulerTransitionError, match="dispatch"):
        complete_dispatch(
            settled,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
        )

    retried = retry_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )
    with pytest.raises(TokenSchedulerTransitionError, match="attempt"):
        complete_dispatch(
            retried.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
        )


def test_stale_cancellation_generation_completion_is_rejected() -> None:
    claim = _claim()
    fenced = claim.snapshot.model_copy(
        update={
            "cancellation_fence": CancellationFence(
                generation=1,
                requested_revision=2,
                state_revision=2,
            ),
            "revision": 2,
        }
    )

    with pytest.raises(TokenSchedulerTransitionError, match="generation"):
        complete_dispatch(
            fenced,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
        )


def _branches() -> tuple[FanOutBranch, ...]:
    return (
        FanOutBranch(node_id="left", inbound_edge_id="edge-left", payload={"branch": 0}),
        FanOutBranch(node_id="right", inbound_edge_id="edge-right", payload={"branch": 1}),
    )


def test_fanout_atomically_retires_parent_and_creates_ordered_children() -> None:
    claim = _claim()

    result = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    assert result.revision == claim.snapshot.revision + 1
    assert result.in_flight_dispatches == ()
    parent, *children = result.tokens
    assert parent.scheduling_state is SchedulingState.SETTLED
    assert tuple(token.current_node_id for token in children) == ("left", "right")
    assert tuple(token.causal_inbound_edge_id for token in children) == (
        "edge-left",
        "edge-right",
    )
    assert tuple(token.payload for token in children) == (
        {"branch": 0},
        {"branch": 1},
    )
    assert result.queue == tuple(children)
    assert result.next_token_ordinal == claim.snapshot.next_token_ordinal + 2
    assert len(result.forks) == 1
    fork = result.forks[0]
    assert fork.lifecycle_state is ForkLifecycleState.OPEN
    assert tuple(child.token_id for child in fork.children) == tuple(
        token.token_id for token in children
    )
    assert tuple(item.child_ordinal for item in fork.obligations) == (0, 1)


def test_fanout_ids_and_order_are_deterministic() -> None:
    first_claim = _claim(_root(payload="same"))
    second_claim = _claim(_root(payload="same"))

    first = fan_out_dispatch(
        first_claim.snapshot,
        dispatch_id=first_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )
    second = fan_out_dispatch(
        second_claim.snapshot,
        dispatch_id=second_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    assert first == second


def test_fanout_replay_returns_persisted_transition_without_new_revision() -> None:
    claim = _claim()
    first = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    replay = fan_out_dispatch(
        first,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    assert replay is first
    assert replay.revision == first.revision
    assert len(replay.tokens) == len(first.tokens)


def test_fanout_replay_uses_creation_identity_after_child_envelope_advances() -> None:
    claim = _claim()
    fanout = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )
    child_claim = claim_next_token(fanout)
    advanced = enqueue_dispatch(
        child_claim.snapshot,
        dispatch_id=child_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        next_node_id="later",
        inbound_edge_id="edge-later",
        payload={"advanced": True},
    )

    replay = fan_out_dispatch(
        advanced,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    assert replay is advanced


def test_fanout_replay_rejects_a_conflicting_creation_plan() -> None:
    claim = _claim()
    persisted = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )
    conflicting = (
        FanOutBranch(node_id="left", inbound_edge_id="edge-left", payload={"branch": 99}),
        _branches()[1],
    )

    with pytest.raises(TokenSchedulerTransitionError, match="dispatch"):
        fan_out_dispatch(
            persisted,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
            branches=conflicting,
        )


def test_active_cancellation_fence_blocks_fanout() -> None:
    claim = _claim()
    fenced = claim.snapshot.model_copy(
        update={
            "cancellation_fence": CancellationFence(
                generation=1,
                requested_revision=2,
                state_revision=2,
            ),
            "revision": 2,
        }
    )

    with pytest.raises(TokenSchedulerTransitionError, match="cancellation"):
        fan_out_dispatch(
            fenced,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
            branches=_branches(),
        )


def test_active_cancellation_fence_blocks_queue_claim() -> None:
    root = _root()
    token = root.tokens[0].model_copy(update={"cancellation_generation": 1})
    fenced = root.model_copy(
        update={
            "revision": 1,
            "queue": (token,),
            "tokens": (token,),
            "cancellation_fence": CancellationFence(
                generation=1,
                requested_revision=1,
                state_revision=1,
            ),
        }
    )

    with pytest.raises(TokenSchedulerTransitionError, match="cancellation"):
        claim_next_token(fenced)


def test_failure_settlement_retires_dispatch_without_losing_envelope() -> None:
    claim = _claim(_root(payload="failure-payload"))

    failed = fail_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    assert failed.tokens[0].payload == claim.dispatch.token.payload
    assert failed.tokens[0].scheduling_state is SchedulingState.SETTLED
    assert failed.in_flight_dispatches == ()


def test_failure_settlement_atomically_resolves_existing_join_obligation() -> None:
    root_claim = _claim(_root(payload="approval-and-durable-child"))
    fanned_out = fan_out_dispatch(
        root_claim.snapshot,
        dispatch_id=root_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=(
            FanOutBranch(
                node_id="durable-child",
                inbound_edge_id="edge-durable",
                payload={"branch": "durable"},
            ),
            FanOutBranch(
                node_id="approval-child",
                inbound_edge_id="edge-approval",
                payload={"branch": "approval"},
            ),
        ),
    )
    fork = fanned_out.forks[0]
    routes = {child.token_id: f"collect-{child.creation_ordinal}" for child in fork.children}

    durable_claim = claim_next_token(fanned_out)
    waiting = deliver_to_join(
        durable_claim.snapshot,
        dispatch_id=durable_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        target_node_id="collect",
        inbound_edge_id=routes[durable_claim.dispatch.token.token_id],
        cohort_inbound_edges=routes,
        payload={"durable": "complete"},
    )
    approval_claim = claim_next_token(waiting)

    failed = fail_dispatch(
        approval_claim.snapshot,
        dispatch_id=approval_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    join = failed.joins[0]
    obligation_by_token = {
        obligation.source_token_id: obligation for obligation in join.obligations
    }
    assert join.lifecycle_state is JoinLifecycleState.READY
    assert (
        obligation_by_token[durable_claim.dispatch.token.token_id].outcome
        is JoinObligationOutcome.DELIVERED
    )
    assert (
        obligation_by_token[approval_claim.dispatch.token.token_id].outcome
        is JoinObligationOutcome.FAILED
    )
    assert (
        next(
            obligation
            for obligation in failed.forks[0].obligations
            if obligation.child_token_id == approval_claim.dispatch.token.token_id
        ).outcome
        is ForkObligationOutcome.FAILED
    )
    assert (
        next(
            token
            for token in failed.tokens
            if token.token_id == approval_claim.dispatch.token.token_id
        ).scheduling_state
        is SchedulingState.SETTLED
    )
    assert failed.in_flight_dispatches == ()
    assert TokenEngineSnapshot.model_validate_json(failed.model_dump_json()) == failed

    cancelled = request_cancellation(failed)
    assert cancelled.state is TokenEngineSnapshotState.CANCELLED
    assert cancelled.joins[0].lifecycle_state is JoinLifecycleState.CANCELLED
    assert TokenEngineSnapshot.model_validate_json(cancelled.model_dump_json()) == cancelled


def _loop_claim() -> DispatchClaim:
    owner = TokenEnvelope(
        token_id="loop-owner",
        current_node_id="before-loop",
        payload={"owner": True},
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        state_revision=0,
        settled_revision=0,
    )
    membership = IterationMembership(
        loop_instance_id="loop-1",
        iteration_frame_id="frame-0",
        loop_header_node_id="loop-header",
        iteration_index=0,
    )
    executing = TokenEnvelope(
        token_id="loop-child",
        parent_token_id=owner.token_id,
        provenance_tag=(ProvenanceFrame(loop_header_node_id="loop-header", iteration_index=0),),
        current_node_id="inside-loop",
        payload={"inside": True},
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.EXECUTING,
        iteration_memberships=(membership,),
        state_revision=0,
    )
    dispatch = InFlightDispatch(
        dispatch_id="loop-dispatch",
        idempotency_key="loop-idempotency",
        token=executing,
        attempt=0,
        cancellation_generation=0,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=0,
        updated_revision=0,
    )
    frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(IterationMember(token_id=executing.token_id, state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=0,
        updated_revision=0,
    )
    loop = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="loop-header",
        enclosing_owner=LoopEnclosingOwner(token_id=owner.token_id),
        frames=(frame,),
        live_child_token_ids=(executing.token_id,),
        next_token_ordinal=1,
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=0,
        updated_revision=0,
    )
    snapshot = TokenEngineSnapshot(
        run_id="run-loop",
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=2,
        tokens=(owner, executing),
        loops=(loop,),
        in_flight_dispatches=(dispatch,),
    )
    return DispatchClaim(snapshot=snapshot, dispatch=dispatch)


def test_fanout_inside_iteration_transfers_every_ownership_membership() -> None:
    claim = _loop_claim()

    result = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=_branches(),
    )

    parent, *children = result.tokens[1:]
    assert parent.scheduling_state is SchedulingState.SETTLED
    assert all(child.iteration_memberships == parent.iteration_memberships for child in children)
    assert all(child.provenance_tag == parent.provenance_tag for child in children)
    loop = result.loops[0]
    assert loop.live_child_token_ids == tuple(sorted(child.token_id for child in children))
    assert loop.next_token_ordinal == 3
    members = loop.frames[0].members
    assert members[0].state is IterationMemberState.INTERNAL_COMPLETION
    assert tuple(member.token_id for member in members[1:]) == tuple(
        child.token_id for child in children
    )
    assert all(member.state is IterationMemberState.ACTIVE for member in members[1:])


@pytest.mark.parametrize(
    ("transition", "expected_state"),
    [
        (complete_dispatch, IterationMemberState.INTERNAL_COMPLETION),
        (fail_dispatch, IterationMemberState.FAILED),
    ],
)
def test_settlement_updates_iteration_ownership_atomically(
    transition: Callable[..., TokenEngineSnapshot],
    expected_state: IterationMemberState,
) -> None:
    claim = _loop_claim()

    result = transition(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    member = result.loops[0].frames[0].members[0]
    assert member.state is expected_state
    assert member.settled_revision == result.revision
    assert result.loops[0].live_child_token_ids == ()


def _nested_fanout_claim(depth: int) -> DispatchClaim:
    claim = _claim()
    for level in range(depth):
        snapshot = fan_out_dispatch(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=claim.dispatch.attempt,
            cancellation_generation=0,
            branches=(
                FanOutBranch(
                    node_id=f"level-{level}",
                    inbound_edge_id=f"edge-{level}",
                    payload={"level": level},
                ),
            ),
        )
        claim = claim_next_token(snapshot)
    return claim


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.parametrize(
    ("settler", "expected_outcome"),
    [
        (complete_dispatch, "suppressed"),
        (fail_dispatch, "failed"),
    ],
)
def test_nested_fork_terminal_outcome_closes_every_ancestor_without_deadlock(
    depth: int,
    settler: Callable[..., TokenEngineSnapshot],
    expected_outcome: str,
) -> None:
    claim = _nested_fanout_claim(depth)

    settled = settler(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    assert settled.queue == ()
    assert settled.in_flight_dispatches == ()
    assert all(fork.lifecycle_state is ForkLifecycleState.CLOSED for fork in settled.forks)
    assert all(fork.outstanding_child_count == 0 for fork in settled.forks)
    assert all(
        obligation.outcome is not None for fork in settled.forks for obligation in fork.obligations
    )
    assert settled.forks[0].obligations[0].outcome.value == expected_outcome


def test_nested_fork_successful_requeue_then_terminal_settlement_closes_ancestors() -> None:
    claim = _nested_fanout_claim(3)
    queued = enqueue_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        next_node_id="final",
        inbound_edge_id="edge-final",
        payload={"success": True},
    )
    final_claim = claim_next_token(queued)

    settled = complete_dispatch(
        final_claim.snapshot,
        dispatch_id=final_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
    )

    assert not any(fork.lifecycle_state is ForkLifecycleState.OPEN for fork in settled.forks)
    assert settled.queue == settled.in_flight_dispatches == ()


class _ContendedStore:
    def __init__(self, snapshot: TokenEngineSnapshot) -> None:
        self.snapshot = snapshot
        self.cas_calls = 0

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
        self.cas_calls += 1
        if self.cas_calls == 1:
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        self.snapshot = snapshot
        return snapshot


def test_cas_coordinator_reloads_transition_but_runs_post_commit_effect_once() -> None:
    store = _ContendedStore(_root())
    transition_calls = 0
    side_effect_calls = 0

    def transition(snapshot: TokenEngineSnapshot | None) -> TokenEngineSnapshot:
        nonlocal transition_calls
        transition_calls += 1
        assert snapshot is not None
        return claim_next_token(snapshot).snapshot

    async def post_commit(snapshot: TokenEngineSnapshot) -> None:
        nonlocal side_effect_calls
        side_effect_calls += 1
        assert snapshot.in_flight_dispatches

    result = asyncio.run(
        apply_token_transition(
            store,
            "run-1",
            transition,
            after_commit=post_commit,
        )
    )

    assert result == store.snapshot
    assert store.cas_calls == 2
    assert transition_calls == 2
    assert side_effect_calls == 1


def test_post_commit_failure_surfaces_committed_snapshot_without_replaying_cas() -> None:
    store = _ContendedStore(_root())
    store.cas_calls = 1  # the next CAS wins immediately
    callback_calls = 0

    def transition(snapshot: TokenEngineSnapshot | None) -> TokenEngineSnapshot:
        assert snapshot is not None
        return claim_next_token(snapshot).snapshot

    async def failing_callback(snapshot: TokenEngineSnapshot) -> None:
        nonlocal callback_calls
        callback_calls += 1
        assert snapshot.in_flight_dispatches
        raise RuntimeError("dispatch handoff failed")

    with pytest.raises(TokenPostCommitError, match="dispatch handoff failed") as caught:
        asyncio.run(
            apply_token_transition(
                store,
                "run-1",
                transition,
                after_commit=failing_callback,
            )
        )

    assert caught.value.committed_snapshot == store.snapshot
    assert callback_calls == 1
    cas_calls_after_commit = store.cas_calls

    def retain_committed(current: TokenEngineSnapshot | None) -> TokenEngineSnapshot:
        assert current is not None
        return current

    recovered = asyncio.run(apply_token_transition(store, "run-1", retain_committed))
    assert recovered == caught.value.committed_snapshot
    assert store.cas_calls == cas_calls_after_commit


def test_public_surface_is_lazy_static_and_cold_importable() -> None:
    repo_root = Path(__file__).parents[3]
    package_source = (repo_root / "src/zeroth/runtime/orchestration/__init__.py").read_text()
    assert "DispatchClaim as DispatchClaim" in package_source
    assert "PostCommitEffect as PostCommitEffect" in package_source
    assert "TokenTransition as TokenTransition" in package_source
    assert '"DispatchClaim": ("token_scheduler", "DispatchClaim")' in package_source
    assert '"PostCommitEffect": ("token_scheduler", "PostCommitEffect")' in package_source
    assert '"TokenTransition": ("token_scheduler", "TokenTransition")' in package_source

    assert callable(TokenTransition)
    assert callable(PostCommitEffect)

    statement = (
        "import sys; import zeroth.runtime.orchestration as package; "
        "assert 'zeroth.runtime.orchestration.token_scheduler' not in sys.modules; "
        "from zeroth.runtime.orchestration import DispatchClaim; "
        "assert DispatchClaim.__module__.endswith('token_scheduler'); "
        "assert not any(name.startswith('zeroth.integrations') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
