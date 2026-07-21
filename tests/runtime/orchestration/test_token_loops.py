"""Pure structured-token loop barriers and replay/CAS behavior."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph import (
    ForkLifecycleState,
    ForkObligationOutcome,
    IterationFrameState,
    IterationMemberState,
    LoopExitRecord,
    LoopLifecycleState,
    SchedulingState,
    TokenEngineSnapshot,
)
from zeroth.contracts.graph.models import JoinConfig
from zeroth.runtime.orchestration import (
    FanOutBranch,
    LoopReductionClaim,
    TokenLoopTransitionError,
    claim_next_token,
    close_ready_loop,
    close_ready_loop_with_cas,
    complete_dispatch,
    enter_loop,
    fan_out_dispatch,
    initialize_token_snapshot,
    reclaim_abandoned_loop_reduction_with_cas,
    settle_loop_member,
)
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError


def _root():
    return initialize_token_snapshot(run_id="loop-run", root_node_id="header", payload={"n": 0})


def _entered():
    root = _root()
    return enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="header",
        body_node_id="body",
        inbound_edge_id="header-body",
        exit_routes={"cross": "outside", "exit-a": "after-a", "exit-b": "after-b"},
    )


def _fanout(width: int = 2):
    entered = _entered()
    claim = claim_next_token(entered)
    return fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=tuple(
            FanOutBranch(
                node_id=f"branch-{index}",
                inbound_edge_id=f"body-{index}",
                payload={"branch": index},
            )
            for index in range(width)
        ),
    )


def _nested_fanout(depth: int) -> TokenEngineSnapshot:
    snapshot = _entered()
    for _ in range(depth):
        level_ids = tuple(item.token_id for item in snapshot.queue)
        for token_id in level_ids:
            while snapshot.queue[0].token_id != token_id:
                snapshot = snapshot.model_copy(
                    update={"queue": (*snapshot.queue[1:], snapshot.queue[0])}
                )
            claim = claim_next_token(snapshot)
            snapshot = fan_out_dispatch(
                claim.snapshot,
                dispatch_id=claim.dispatch.dispatch_id,
                attempt=0,
                cancellation_generation=0,
                branches=(
                    FanOutBranch(
                        node_id=f"nested-{depth}-left",
                        inbound_edge_id=f"nested-{depth}-left",
                        payload="left",
                    ),
                    FanOutBranch(
                        node_id=f"nested-{depth}-right",
                        inbound_edge_id=f"nested-{depth}-right",
                        payload="right",
                    ),
                ),
            )
    return snapshot


def _forked_loop(
    *, nested_depth: int = 1, exit_routes: dict[str, str] | None = None
) -> tuple[TokenEngineSnapshot, str]:
    snapshot = _root()
    for depth in range(nested_depth):
        claim = claim_next_token(snapshot)
        snapshot = fan_out_dispatch(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
            branches=(
                FanOutBranch(
                    node_id=f"fork-{depth}",
                    inbound_edge_id=f"fork-{depth}",
                    payload=depth,
                ),
            ),
        )
    owner = snapshot.queue[0]
    entered = enter_loop(
        snapshot,
        token_id=owner.token_id,
        loop_header_node_id=owner.current_node_id,
        body_node_id="loop-body",
        inbound_edge_id="loop-body",
        exit_routes=exit_routes or {"exit": "target"},
    )
    return entered, entered.queue[0].token_id


def _multi_exit_ready(count: int) -> TokenEngineSnapshot:
    routes = {f"exit-{index}": f"target-{index}" for index in range(count)}
    snapshot, _ = _forked_loop(exit_routes=routes)
    for index in range(count - 1):
        claim = claim_next_token(snapshot)
        snapshot = fan_out_dispatch(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
            branches=(
                FanOutBranch(node_id="exit-branch", inbound_edge_id="exit-branch", payload=index),
                FanOutBranch(node_id="back-branch", inbound_edge_id="back-branch", payload=index),
            ),
        )
        exit_token, back_token = (item.token_id for item in snapshot.queue[-2:])
        snapshot = settle_loop_member(
            snapshot,
            token_id=exit_token,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id=f"exit-{index}",
            target_node_id=f"target-{index}",
            payload=index,
        )
        snapshot = settle_loop_member(
            snapshot,
            token_id=back_token,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=index + 1,
        )
        snapshot = close_ready_loop(snapshot, snapshot.loops[-1].loop_instance_id)
    return settle_loop_member(
        snapshot,
        token_id=snapshot.queue[-1].token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id=f"exit-{count - 1}",
        target_node_id=f"target-{count - 1}",
        payload=count - 1,
    )


def test_enter_loop_is_deterministic_atomic_and_full_fingerprint_replay() -> None:
    root = _root()
    entered = _entered()
    loop = entered.loops[0]
    child = entered.queue[0]

    assert entered.revision == root.revision + 1
    assert root.tokens[0].token_id == loop.enclosing_owner.token_id
    assert (
        next(
            token for token in entered.tokens if token.token_id == root.tokens[0].token_id
        ).scheduling_state
        is SchedulingState.SETTLED
    )
    assert loop.frames[0].state is IterationFrameState.ACTIVE
    assert loop.frames[0].members[0].token_id == child.token_id
    assert child.iteration_memberships[-1].iteration_frame_id == loop.frames[0].iteration_frame_id
    assert (
        enter_loop(
            entered,
            token_id=root.tokens[0].token_id,
            loop_header_node_id="header",
            body_node_id="body",
            inbound_edge_id="header-body",
            exit_routes={"cross": "outside", "exit-a": "after-a", "exit-b": "after-b"},
        )
        is entered
    )

    with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
        enter_loop(
            entered,
            token_id=root.tokens[0].token_id,
            loop_header_node_id="header",
            body_node_id="different",
            inbound_edge_id="header-body",
            exit_routes={"cross": "outside", "exit-a": "after-a", "exit-b": "after-b"},
        )


def test_one_of_n_settlements_waits_and_last_member_readies_barrier() -> None:
    snapshot = _fanout(3)
    child_ids = tuple(child.token_id for child in snapshot.forks[0].children)
    first = settle_loop_member(
        snapshot, token_id=child_ids[2], outcome=IterationMemberState.INTERNAL_COMPLETION
    )
    assert first.loops[0].frames[-1].state is IterationFrameState.ACTIVE

    second = settle_loop_member(
        first, token_id=child_ids[0], outcome=IterationMemberState.INTERNAL_COMPLETION
    )
    ready = settle_loop_member(
        second, token_id=child_ids[1], outcome=IterationMemberState.INTERNAL_COMPLETION
    )
    assert ready.loops[0].frames[-1].state is IterationFrameState.BARRIER_READY
    assert ready.loops[0].live_child_token_ids == ()


def test_multiple_back_edges_reduce_in_canonical_order_and_advance_once() -> None:
    snapshot = _fanout(2)
    child_ids = tuple(child.token_id for child in snapshot.forks[0].children)
    snapshot = settle_loop_member(
        snapshot,
        token_id=child_ids[1],
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back-z",
        payload=None,
    )
    ready = settle_loop_member(
        snapshot,
        token_id=child_ids[0],
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back-a",
        payload={"value": 1},
    )

    observed: list[tuple[str, object]] = []

    def reducer(config, inputs):
        assert config.merge_strategy == "collect"
        observed.extend((item.inbound_edge_id, item.payload) for item in inputs)
        return {"ordered": [item.payload for item in inputs]}

    advanced = close_ready_loop(
        ready,
        ready.loops[0].loop_instance_id,
        continuation_config=JoinConfig(),
        reducer=reducer,
    )
    loop = advanced.loops[0]
    assert observed == [("back-a", {"value": 1}), ("back-z", None)]
    assert tuple(frame.state for frame in loop.frames) == (
        IterationFrameState.SETTLED,
        IterationFrameState.ACTIVE,
    )
    assert loop.frames[-1].iteration_index == 1
    assert advanced.queue[-1].provenance_tag[-1].iteration_index == 1
    assert (
        close_ready_loop(
            advanced,
            loop.loop_instance_id,
            continuation_config=JoinConfig(),
            reducer=lambda *_: (_ for _ in ()).throw(AssertionError("reduced twice")),
        )
        is advanced
    )


def test_multiple_back_edges_require_explicit_join_config() -> None:
    snapshot = _fanout(2)
    for child in snapshot.forks[0].children:
        snapshot = settle_loop_member(
            snapshot,
            token_id=child.token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=child.creation_ordinal,
        )
    with pytest.raises(TokenLoopTransitionError, match="JoinConfig"):
        close_ready_loop(snapshot, snapshot.loops[0].loop_instance_id)


def test_continue_and_exit_accumulates_without_early_emission_then_finalizes_distinct_exits() -> (
    None
):
    snapshot = _fanout(2)
    first, second = (child.token_id for child in snapshot.forks[0].children)
    snapshot = settle_loop_member(
        snapshot,
        token_id=first,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit-a",
        target_node_id="after-a",
        payload=None,
    )
    ready = settle_loop_member(
        snapshot,
        token_id=second,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload={"again": True},
    )
    advanced = close_ready_loop(ready, ready.loops[0].loop_instance_id)
    assert all(token.current_node_id != "after-a" for token in advanced.queue)
    exit_a = next(item for item in advanced.loops[0].exits if item.exit_edge_id == "exit-a")
    assert exit_a.records[0].delivery is not None

    current = advanced.queue[-1]
    ready_final = settle_loop_member(
        advanced,
        token_id=current.token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit-b",
        target_node_id="after-b",
        payload={"done": 2},
    )
    completed = close_ready_loop(ready_final, ready_final.loops[0].loop_instance_id)
    loop = completed.loops[0]
    assert loop.lifecycle_state is LoopLifecycleState.COMPLETED
    assert {token.current_node_id for token in completed.queue} == {"after-a", "after-b"}
    assert len(loop.emitted_continuation_token_ids) == 2
    assert next(
        token for token in completed.queue if token.current_node_id == "after-a"
    ).model_dump(mode="json")["payload"] == [None]


def test_same_exit_payloads_freeze_in_child_order_and_replay_exactly() -> None:
    snapshot = _fanout(3)
    children = snapshot.forks[0].children
    for child in reversed(children):
        snapshot = settle_loop_member(
            snapshot,
            token_id=child.token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit-a",
            target_node_id="after-a",
            payload={"fingerprint": child.creation_ordinal},
        )

    completed = close_ready_loop(snapshot, snapshot.loops[0].loop_instance_id)
    restored = TokenEngineSnapshot.model_validate_json(completed.model_dump_json())
    exit_state = next(item for item in restored.loops[0].exits if item.exit_edge_id == "exit-a")

    assert tuple(record.canonical_order.child_ordinal for record in exit_state.records) == (0, 1, 2)
    assert tuple(
        record.delivery.payload for record in exit_state.records if record.delivery is not None
    ) == (
        {"fingerprint": 0},
        {"fingerprint": 1},
        {"fingerprint": 2},
    )
    assert restored.queue[0].model_dump(mode="json")["payload"] == [
        {"fingerprint": 0},
        {"fingerprint": 1},
        {"fingerprint": 2},
    ]
    assert close_ready_loop(restored, restored.loops[0].loop_instance_id) is restored


def test_all_suppressed_loop_completes_without_delivery() -> None:
    entered = _entered()
    token = entered.queue[0]
    ready = settle_loop_member(
        entered,
        token_id=token.token_id,
        outcome=IterationMemberState.SUPPRESSED,
        edge_id="exit-a",
        target_node_id="after-a",
    )
    completed = close_ready_loop(ready, ready.loops[0].loop_instance_id)
    assert completed.queue == ()
    assert completed.loops[0].lifecycle_state is LoopLifecycleState.COMPLETED
    assert completed.loops[0].emitted_continuation_token_ids == ()


def test_nested_loop_finalization_transfers_outer_member_to_inner_continuation() -> None:
    outer = _entered()
    outer_child = outer.queue[0]
    inner = enter_loop(
        outer,
        token_id=outer_child.token_id,
        loop_header_node_id="body",
        body_node_id="inner-body",
        inbound_edge_id="body-inner",
        exit_routes={"inner-exit": "outer-body"},
    )
    inner_loop = inner.loops[-1]
    ready = settle_loop_member(
        inner,
        token_id=inner.queue[0].token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="inner-exit",
        target_node_id="outer-body",
        payload={"inner": "done"},
    )
    completed = close_ready_loop(ready, inner_loop.loop_instance_id)
    outer_loop = completed.loops[0]
    continuation = completed.queue[0]
    members = {item.token_id: item for item in outer_loop.frames[-1].members}
    assert members[outer_child.token_id].state is IterationMemberState.INTERNAL_COMPLETION
    assert members[continuation.token_id].state is IterationMemberState.ACTIVE
    assert continuation.iteration_memberships == outer_child.iteration_memberships


def test_cross_scope_exit_settles_each_crossed_frame_but_preserves_sibling() -> None:
    outer = _fanout(2)
    first, sibling = (child.token_id for child in outer.forks[0].children)
    inner = enter_loop(
        outer,
        token_id=first,
        loop_header_node_id="branch-0",
        body_node_id="inner",
        inbound_edge_id="to-inner",
        exit_routes={"cross": "outside"},
    )
    crossing = inner.queue[-1]
    crossed = settle_loop_member(
        inner,
        token_id=crossing.token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="cross",
        target_node_id="outside",
        payload={"crossed": True},
        crossed_loop_instance_ids=tuple(loop.loop_instance_id for loop in inner.loops),
    )
    assert sibling in crossed.loops[0].live_child_token_ids
    assert crossed.loops[1].frames[-1].state is IterationFrameState.BARRIER_READY
    assert crossed.loops[0].frames[-1].state is IterationFrameState.ACTIVE


def test_cross_scope_exit_can_finalize_inner_before_outer_and_replay() -> None:
    outer = _fanout(2)
    first, sibling = (child.token_id for child in outer.forks[0].children)
    inner = enter_loop(
        outer,
        token_id=first,
        loop_header_node_id="branch-0",
        body_node_id="inner",
        inbound_edge_id="to-inner",
        exit_routes={"cross": "outside"},
    )
    crossing = inner.queue[-1]
    crossed = settle_loop_member(
        inner,
        token_id=crossing.token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="cross",
        target_node_id="outside",
        payload="inner",
        crossed_loop_instance_ids=tuple(loop.loop_instance_id for loop in inner.loops),
    )
    outer_loop = crossed.loops[0]
    outer_frame = outer_loop.frames[-1]
    contradictory_frame = outer_frame.model_copy(
        update={
            "members": tuple(
                member.model_copy(update={"settlement_command_fingerprint": "0" * 64})
                if member.settlement_command_fingerprint is not None
                else member
                for member in outer_frame.members
            )
        }
    )
    contradictory_outer = outer_loop.model_copy(
        update={"frames": (*outer_loop.frames[:-1], contradictory_frame)}
    )
    contradictory = crossed.model_construct(
        **{
            **{name: getattr(crossed, name) for name in type(crossed).model_fields},
            "loops": (contradictory_outer, *crossed.loops[1:]),
        }
    )
    with pytest.raises(TokenLoopTransitionError, match="contradicts persisted"):
        close_ready_loop(contradictory, inner.loops[-1].loop_instance_id)
    finalized_inner = close_ready_loop(crossed, inner.loops[-1].loop_instance_id)
    assert finalized_inner.loops[-1].lifecycle_state is LoopLifecycleState.COMPLETED
    assert close_ready_loop(finalized_inner, inner.loops[-1].loop_instance_id) is finalized_inner

    outer_ready = settle_loop_member(
        finalized_inner,
        token_id=sibling,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="cross",
        target_node_id="outside",
        payload="sibling",
    )
    completed = close_ready_loop(outer_ready, outer.loops[0].loop_instance_id)
    assert completed.queue[0].model_dump(mode="json")["payload"] == ["inner", "sibling"]


def test_fanout_inside_loop_end_to_end_with_different_settlement_order() -> None:
    first = _fanout(3)
    ids = tuple(child.token_id for child in first.forks[0].children)
    for token_id in reversed(ids):
        first = settle_loop_member(
            first,
            token_id=token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit-a",
            target_node_id="after-a",
            payload=token_id,
        )
    closed = close_ready_loop(first, first.loops[0].loop_instance_id)
    payload = closed.queue[0].model_dump(mode="json")["payload"]
    assert payload == list(ids)


def test_loop_exit_inside_enclosing_fork_transfers_exact_open_slot() -> None:
    entered, token_id = _forked_loop()
    ready = settle_loop_member(
        entered,
        token_id=token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit",
        target_node_id="target",
        payload="inside",
    )
    assert (
        settle_loop_member(
            ready,
            token_id=token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit",
            target_node_id="target",
            payload="inside",
        )
        is ready
    )
    completed = close_ready_loop(ready, ready.loops[-1].loop_instance_id)
    continuation = completed.queue[0]
    fork = completed.forks[0]
    assert continuation.fork_lineage == ready.tokens[-1].fork_lineage
    assert fork.children[0].token_id == continuation.token_id
    assert fork.obligations[0].child_token_id == continuation.token_id
    assert fork.obligations[0].outcome is None
    assert TokenEngineSnapshot.model_validate_json(completed.model_dump_json()) == completed


@pytest.mark.parametrize("crossed_count", [1, 2])
def test_loop_exit_crosses_exact_fork_suffix(crossed_count: int) -> None:
    entered, token_id = _forked_loop(nested_depth=2)
    token = next(item for item in entered.tokens if item.token_id == token_id)
    crossed = tuple(frame.fork_id for frame in token.fork_lineage[-crossed_count:])
    ready = settle_loop_member(
        entered,
        token_id=token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit",
        target_node_id="target",
        payload=crossed_count,
        crossed_fork_ids=crossed,
    )
    assert (
        settle_loop_member(
            ready,
            token_id=token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit",
            target_node_id="target",
            payload=crossed_count,
            crossed_fork_ids=crossed,
        )
        is ready
    )
    contradictory = tuple(frame.fork_id for frame in token.fork_lineage)
    if contradictory != crossed:
        with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
            settle_loop_member(
                ready,
                token_id=token_id,
                outcome=IterationMemberState.EXIT_DELIVERY,
                edge_id="exit",
                target_node_id="target",
                payload=crossed_count,
                crossed_fork_ids=contradictory,
            )
    completed = close_ready_loop(ready, ready.loops[-1].loop_instance_id)
    continuation = completed.queue[0]
    assert tuple(frame.fork_id for frame in continuation.fork_lineage) == tuple(
        frame.fork_id for frame in token.fork_lineage[:-crossed_count]
    )
    for fork_id in crossed:
        fork = next(item for item in completed.forks if item.fork_id == fork_id)
        assert fork.obligations[0].outcome is ForkObligationOutcome.EXITED
    if continuation.fork_lineage:
        owner = next(
            item
            for item in completed.forks
            if item.fork_id == continuation.fork_lineage[-1].fork_id
        )
        assert owner.children[0].token_id == continuation.token_id
        assert owner.obligations[0].outcome is None
    assert TokenEngineSnapshot.model_validate_json(completed.model_dump_json()) == completed


@pytest.mark.parametrize("exit_count", [2, 3])
def test_multiple_internal_exits_create_one_nested_exit_fork(exit_count: int) -> None:
    ready = _multi_exit_ready(exit_count)
    completed = close_ready_loop(ready, ready.loops[-1].loop_instance_id)
    restored = TokenEngineSnapshot.model_validate_json(completed.model_dump_json())
    assert close_ready_loop(restored, restored.loops[-1].loop_instance_id) is restored
    assert {item.current_node_id for item in restored.queue} == {
        f"target-{index}" for index in range(exit_count)
    }
    outer, nested = restored.forks[0], restored.forks[-1]
    assert nested.parent_fork_id == outer.fork_id
    assert nested.parent_token_id == outer.children[0].token_id
    assert outer.outstanding_child_count == 1
    assert len({item.token_id for item in outer.children}) == 1
    assert tuple(item.token_id for item in nested.children) == tuple(
        item.token_id for item in restored.queue
    )

    snapshot = restored
    while snapshot.queue:
        claim = claim_next_token(snapshot)
        snapshot = complete_dispatch(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=0,
        )
    assert all(fork.lifecycle_state is ForkLifecycleState.CLOSED for fork in snapshot.forks)
    assert all(fork.outstanding_child_count == 0 for fork in snapshot.forks)


def test_loop_exit_record_requires_exact_fork_lineage_partition() -> None:
    ready = _multi_exit_ready(2)
    record = next(
        exit_state.records[0] for exit_state in ready.loops[-1].exits if exit_state.records
    )
    dumped = record.model_dump(mode="json")
    dumped["crossed_fork_ids"] = ["foreign-fork"]
    with pytest.raises(ValidationError, match="partition"):
        LoopExitRecord.model_validate_json(json.dumps(dumped))

    dumped = record.model_dump(mode="json")
    dumped["surviving_fork_lineage"] = []
    with pytest.raises(ValidationError, match="partition"):
        LoopExitRecord.model_validate_json(json.dumps(dumped))

    for field, value in (
        ("child_ordinal", record.surviving_fork_lineage[0].child_ordinal + 1),
        ("parent_fork_id", "foreign-parent"),
        ("join_instance_id", "foreign-join"),
    ):
        dumped = record.model_dump(mode="json")
        dumped["surviving_fork_lineage"][0][field] = value
        with pytest.raises(ValidationError, match="partition"):
            LoopExitRecord.model_validate_json(json.dumps(dumped))


def test_failure_policy_is_scope_local_and_best_effort_requires_permission() -> None:
    snapshot = _fanout(2)
    first, sibling = (child.token_id for child in snapshot.forks[0].children)
    with pytest.raises(TokenLoopTransitionError, match="explicitly permit"):
        settle_loop_member(
            snapshot,
            token_id=first,
            outcome=IterationMemberState.FAILED,
            failure_mode="best_effort",
        )
    scoped = settle_loop_member(
        snapshot,
        token_id=first,
        outcome=IterationMemberState.FAILED,
        failure_mode="fail_fast",
    )
    assert scoped.cancellation_fence is None
    members = {item.token_id: item for item in scoped.loops[0].frames[-1].members}
    assert members[sibling].state is IterationMemberState.CANCELLED
    assert scoped.forks[0].outstanding_child_count == 0
    assert scoped.forks[0].lifecycle_state.value == "closed"


def test_scoped_cancellation_cancels_siblings_without_run_fence() -> None:
    snapshot = _fanout(2)
    first, sibling = (child.token_id for child in snapshot.forks[0].children)
    cancelled = settle_loop_member(
        snapshot,
        token_id=first,
        outcome=IterationMemberState.CANCELLED,
        failure_mode="fail_fast",
    )
    assert cancelled.cancellation_fence is None
    assert (
        next(token for token in cancelled.tokens if token.token_id == sibling).scheduling_state
        is SchedulingState.SETTLED
    )
    assert cancelled.forks[0].outstanding_child_count == 0


@pytest.mark.parametrize("depth", [2, 3])
def test_fail_fast_inside_nested_fanout_closes_every_fork(depth: int) -> None:
    snapshot = _nested_fanout(depth)
    failed = settle_loop_member(
        snapshot,
        token_id=snapshot.queue[-1].token_id,
        outcome=IterationMemberState.FAILED,
        failure_mode="fail_fast",
    )
    assert failed.queue == ()
    assert all(fork.lifecycle_state is ForkLifecycleState.CLOSED for fork in failed.forks)
    assert all(fork.outstanding_child_count == 0 for fork in failed.forks)
    assert all(
        obligation.outcome is not None for fork in failed.forks for obligation in fork.obligations
    )


class _CASStore:
    def __init__(self, snapshot, *, conflicts: int = 0):
        self.snapshot = snapshot
        self.conflicts = conflicts

    async def get_token_snapshot(self, run_id: str):
        assert run_id == self.snapshot.run_id
        return self.snapshot

    async def compare_and_swap_token_snapshot(self, run_id: str, *, expected_revision, snapshot):
        assert run_id == self.snapshot.run_id
        if self.conflicts:
            self.conflicts -= 1
            raise TokenSnapshotConcurrencyError(
                "contended",
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        if expected_revision != self.snapshot.revision:
            raise TokenSnapshotConcurrencyError(
                "stale",
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        self.snapshot = snapshot
        return snapshot


def test_cas_claim_evaluates_reducer_once_and_survives_contention() -> None:
    snapshot = _fanout(2)
    for child in snapshot.forks[0].children:
        snapshot = settle_loop_member(
            snapshot,
            token_id=child.token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=child.creation_ordinal,
        )
    store = _CASStore(snapshot, conflicts=1)
    calls = 0

    def reducer(_config, inputs):
        nonlocal calls
        calls += 1
        return [item.payload for item in inputs]

    result = asyncio.run(
        close_ready_loop_with_cas(
            store,
            snapshot.run_id,
            snapshot.loops[0].loop_instance_id,
            continuation_config=JoinConfig(),
            reducer=reducer,
            claim_owner_id="worker-a",
        )
    )
    assert calls == 1
    assert result.loops[0].frames[-1].iteration_index == 1


def test_pure_and_cas_closure_share_optional_config_semantics() -> None:
    single = _entered()
    single = settle_loop_member(
        single,
        token_id=single.queue[0].token_id,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload={"single": True},
    )
    pure_single = close_ready_loop(single, single.loops[0].loop_instance_id)
    cas_single = asyncio.run(
        close_ready_loop_with_cas(
            _CASStore(single), single.run_id, single.loops[0].loop_instance_id
        )
    )
    assert pure_single.queue[-1].payload == cas_single.queue[-1].payload == {"single": True}

    many = _fanout(2)
    for child in many.forks[0].children:
        many = settle_loop_member(
            many,
            token_id=child.token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=child.creation_ordinal,
        )
    with pytest.raises(TokenLoopTransitionError, match="JoinConfig"):
        close_ready_loop(many, many.loops[0].loop_instance_id)
    with pytest.raises(TokenLoopTransitionError, match="JoinConfig"):
        asyncio.run(
            close_ready_loop_with_cas(_CASStore(many), many.run_id, many.loops[0].loop_instance_id)
        )
    config = JoinConfig()
    pure_many = close_ready_loop(many, many.loops[0].loop_instance_id, continuation_config=config)
    cas_many = asyncio.run(
        close_ready_loop_with_cas(
            _CASStore(many),
            many.run_id,
            many.loops[0].loop_instance_id,
            continuation_config=config,
        )
    )
    assert pure_many.queue[-1].payload == cas_many.queue[-1].payload


def test_abandoned_claim_recovery_is_explicit_and_stale_fenced() -> None:
    snapshot = _entered()
    ready = settle_loop_member(
        snapshot,
        token_id=snapshot.queue[0].token_id,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload=1,
    )
    store = _CASStore(ready)

    async def crash_reducer(_config, _inputs):
        raise AssertionError

    # A claimed snapshot is observable when a closer crashes after winning the claim.
    async def claim_only():
        from zeroth.runtime.orchestration.token_loop_claims import _claim_loop_with_cas

        return await _claim_loop_with_cas(
            store,
            ready.run_id,
            ready.loops[0].loop_instance_id,
            JoinConfig(),
            owner_id="dead-worker",
            max_attempts=2,
        )

    claim = asyncio.run(claim_only())
    observed = LoopReductionClaim.from_loop(store.snapshot.loops[0])
    assert claim == observed
    replacement = asyncio.run(
        reclaim_abandoned_loop_reduction_with_cas(
            store,
            ready.run_id,
            ready.loops[0].loop_instance_id,
            observed_claim=observed,
            new_owner_id="recovery-worker",
        )
    )
    assert replacement.attempt == observed.attempt + 1
    with pytest.raises(TokenLoopTransitionError, match="claim changed"):
        asyncio.run(
            reclaim_abandoned_loop_reduction_with_cas(
                store,
                ready.run_id,
                ready.loops[0].loop_instance_id,
                observed_claim=observed,
                new_owner_id="stale-worker",
            )
        )


def test_runtime_loop_exports_are_lazy_and_cold_import_safe() -> None:
    root = Path(__file__).resolve().parents[3]
    code = """
import sys
import zeroth.runtime.orchestration as orchestration
assert 'zeroth.runtime.orchestration.driver' not in sys.modules
assert orchestration.enter_loop.__module__.endswith('token_loops')
assert 'zeroth.runtime.orchestration.driver' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_member_settlement_exact_replay_is_unchanged_and_conflict_fails() -> None:
    entered = _entered()
    token_id = entered.queue[0].token_id
    settled = settle_loop_member(
        entered,
        token_id=token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit-a",
        target_node_id="after-a",
        payload={"stable": True},
    )
    assert (
        settle_loop_member(
            settled,
            token_id=token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit-a",
            target_node_id="after-a",
            payload={"stable": True},
        )
        is settled
    )
    with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
        settle_loop_member(
            settled,
            token_id=token_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="exit-a",
            target_node_id="after-a",
            payload={"stable": False},
        )


def test_settlement_replay_for_non_loop_token_is_typed_error() -> None:
    snapshot = _fanout(1)
    non_loop_token_id = snapshot.tokens[0].token_id
    with pytest.raises(TokenLoopTransitionError, match="not owned by an iteration frame"):
        settle_loop_member(
            snapshot,
            token_id=non_loop_token_id,
            outcome=IterationMemberState.INTERNAL_COMPLETION,
        )


def test_best_effort_suppression_without_exit_record_replays_exactly() -> None:
    entered = _entered()
    token_id = entered.queue[0].token_id
    settled = settle_loop_member(
        entered,
        token_id=token_id,
        outcome=IterationMemberState.FAILED,
        failure_mode="best_effort",
        allow_failure_suppression=True,
    )
    assert (
        settle_loop_member(
            settled,
            token_id=token_id,
            outcome=IterationMemberState.FAILED,
            failure_mode="best_effort",
            allow_failure_suppression=True,
        )
        is settled
    )
    with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
        settle_loop_member(
            settled,
            token_id=token_id,
            outcome=IterationMemberState.FAILED,
            edge_id="exit-a",
            target_node_id="after-a",
            failure_mode="best_effort",
            allow_failure_suppression=True,
        )


def test_crash_boundaries_round_trip_without_recomputation() -> None:
    entered = _entered()
    ready = settle_loop_member(
        entered,
        token_id=entered.queue[0].token_id,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload={"iteration": 1},
    )
    restored_ready = TokenEngineSnapshot.model_validate_json(ready.model_dump_json())
    advanced = close_ready_loop(restored_ready, restored_ready.loops[0].loop_instance_id)
    restored_advanced = TokenEngineSnapshot.model_validate_json(advanced.model_dump_json())
    assert (
        close_ready_loop(restored_advanced, advanced.loops[0].loop_instance_id) is restored_advanced
    )

    final_ready = settle_loop_member(
        restored_advanced,
        token_id=restored_advanced.queue[0].token_id,
        outcome=IterationMemberState.EXIT_DELIVERY,
        edge_id="exit-a",
        target_node_id="after-a",
        payload="done",
    )
    completed = close_ready_loop(final_ready, final_ready.loops[0].loop_instance_id)
    restored_completed = TokenEngineSnapshot.model_validate_json(completed.model_dump_json())
    assert (
        close_ready_loop(restored_completed, completed.loops[0].loop_instance_id)
        is restored_completed
    )


def test_iteration_history_compacts_to_two_frames_without_orphan_memberships() -> None:
    snapshot = _entered()
    for iteration in range(5):
        snapshot = settle_loop_member(
            snapshot,
            token_id=snapshot.queue[-1].token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=iteration,
        )
        snapshot = close_ready_loop(snapshot, snapshot.loops[0].loop_instance_id)
    assert len(snapshot.loops[0].frames) == 2
    assert tuple(item.iteration_index for item in snapshot.loops[0].frames) == (4, 5)
    TokenEngineSnapshot.model_validate_json(snapshot.model_dump_json())


def test_failed_reducer_releases_claim_for_explicit_retry() -> None:
    snapshot = _fanout(2)
    for child in snapshot.forks[0].children:
        snapshot = settle_loop_member(
            snapshot,
            token_id=child.token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=child.creation_ordinal,
        )
    store = _CASStore(snapshot)

    def broken(_config, _inputs):
        raise RuntimeError("reducer crashed")

    with pytest.raises(RuntimeError, match="reducer crashed"):
        asyncio.run(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                continuation_config=JoinConfig(),
                reducer=broken,
                claim_owner_id="worker-broken",
            )
        )
    assert store.snapshot.loops[0].reduction_claim_id is None
    assert store.snapshot.loops[0].reduction_attempt == 1

    recovered = asyncio.run(
        close_ready_loop_with_cas(
            store,
            snapshot.run_id,
            snapshot.loops[0].loop_instance_id,
            continuation_config=JoinConfig(),
            reducer=lambda _config, inputs: [item.payload for item in inputs],
            claim_owner_id="worker-retry",
        )
    )
    assert recovered.loops[0].frames[-1].iteration_index == 1


def test_malformed_loop_membership_is_rejected_on_restore() -> None:
    entered = _entered()
    dumped = entered.model_dump(mode="json")
    dumped["tokens"][-1]["iteration_memberships"][-1]["iteration_frame_id"] = "missing"
    dumped["queue"][-1]["iteration_memberships"][-1]["iteration_frame_id"] = "missing"
    with pytest.raises(ValueError, match="missing loop/frame"):
        TokenEngineSnapshot.model_validate_json(json.dumps(dumped))


def test_dispatch_owned_entry_consumes_exact_attempt_and_generation() -> None:
    root = _root()
    claimed = claim_next_token(root)
    entered = enter_loop(
        claimed.snapshot,
        token_id=claimed.dispatch.token.token_id,
        dispatch_id=claimed.dispatch.dispatch_id,
        attempt=claimed.dispatch.attempt,
        cancellation_generation=claimed.dispatch.cancellation_generation,
        loop_header_node_id="header",
        body_node_id="body",
        inbound_edge_id="header-body",
        exit_routes={"exit-a": "after-a"},
    )
    assert entered.in_flight_dispatches == ()
    with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
        enter_loop(
            entered,
            token_id=claimed.dispatch.token.token_id,
            dispatch_id=claimed.dispatch.dispatch_id,
            attempt=claimed.dispatch.attempt + 1,
            cancellation_generation=claimed.dispatch.cancellation_generation,
            loop_header_node_id="header",
            body_node_id="body",
            inbound_edge_id="header-body",
            exit_routes={"exit-a": "after-a"},
        )


def test_dispatch_owned_settlement_is_fenced_and_exactly_replayable() -> None:
    entered = _entered()
    claimed = claim_next_token(entered)
    settled = settle_loop_member(
        claimed.snapshot,
        token_id=claimed.dispatch.token.token_id,
        dispatch_id=claimed.dispatch.dispatch_id,
        attempt=claimed.dispatch.attempt,
        cancellation_generation=claimed.dispatch.cancellation_generation,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload="again",
    )
    assert settled.in_flight_dispatches == ()
    assert (
        settle_loop_member(
            settled,
            token_id=claimed.dispatch.token.token_id,
            dispatch_id=claimed.dispatch.dispatch_id,
            attempt=claimed.dispatch.attempt,
            cancellation_generation=claimed.dispatch.cancellation_generation,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload="again",
        )
        is settled
    )
    with pytest.raises(TokenLoopTransitionError, match="replay contradicts"):
        settle_loop_member(
            settled,
            token_id=claimed.dispatch.token.token_id,
            dispatch_id=claimed.dispatch.dispatch_id,
            attempt=claimed.dispatch.attempt + 1,
            cancellation_generation=claimed.dispatch.cancellation_generation,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload="again",
        )
