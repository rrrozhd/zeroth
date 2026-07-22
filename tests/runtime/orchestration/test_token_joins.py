"""Cohort-aware durable join transitions."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import zeroth.runtime.orchestration as orchestration

from zeroth.contracts.graph import (
    DispatchLifecycleState,
    ForkLifecycleState,
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
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.contracts.graph.models import JoinConfig
from zeroth.runtime.orchestration import (
    FanOutBranch,
    JoinReducerInput,
    TokenJoinTransitionError,
    apply_token_transition,
    claim_next_token,
    close_ready_join,
    close_ready_join_with_cas,
    deliver_to_join,
    fan_out_dispatch,
    initialize_token_snapshot,
    reduce_join_inputs,
    settle_join_without_delivery,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
)


def _fanout(width: int = 3, *, snapshot: TokenEngineSnapshot | None = None) -> TokenEngineSnapshot:
    initial = snapshot or initialize_token_snapshot(
        run_id="join-run", root_node_id="entry", payload={"root": True}
    )
    claim = claim_next_token(initial)
    return fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=tuple(
            FanOutBranch(node_id=f"branch-{i}", inbound_edge_id=f"split-{i}", payload={"i": i})
            for i in range(width)
        ),
    )


def _routes(snapshot: TokenEngineSnapshot, target: str = "join") -> Mapping[str, str]:
    fork = snapshot.forks[-1]
    return {child.token_id: f"{target}-edge-{child.creation_ordinal}" for child in fork.children}


def _deliver_head(
    snapshot: TokenEngineSnapshot,
    *,
    payload: object,
    target: str = "join",
) -> TokenEngineSnapshot:
    routes = _routes(snapshot, target)
    claim = claim_next_token(snapshot)
    token_id = claim.dispatch.token.token_id
    return deliver_to_join(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=claim.dispatch.attempt,
        cancellation_generation=claim.dispatch.cancellation_generation,
        target_node_id=target,
        inbound_edge_id=routes[token_id],
        cohort_inbound_edges=routes,
        payload=payload,
    )


@pytest.mark.parametrize("width", [2, 3, 6])
def test_arrivals_create_one_cohort_join_and_never_ready_early(width: int) -> None:
    snapshot = _fanout(width)
    for index in range(width):
        snapshot = _deliver_head(snapshot, payload={"arrival": index})
        join = snapshot.joins[0]
        expected = JoinLifecycleState.READY if index == width - 1 else JoinLifecycleState.OPEN
        assert join.lifecycle_state is expected
        assert len(snapshot.joins) == 1
        assert [item.child_ordinal for item in join.obligations] == list(range(width))
        assert not any(token.current_node_id == "join" for token in snapshot.queue)


def test_arrival_replay_is_idempotent_and_conflict_is_loud() -> None:
    start = _fanout(2)
    routes = _routes(start)
    claim = claim_next_token(start)
    kwargs = dict(
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        target_node_id="join",
        inbound_edge_id=routes[claim.dispatch.token.token_id],
        cohort_inbound_edges=routes,
        payload=None,
    )
    arrived = deliver_to_join(claim.snapshot, **kwargs)
    assert deliver_to_join(arrived, **kwargs) is arrived
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        deliver_to_join(arrived, **{**kwargs, "payload": {"changed": True}})
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        deliver_to_join(arrived, **{**kwargs, "failure_mode": "best_effort"})
    wrong_routes = {token_id: f"wrong-{edge_id}" for token_id, edge_id in routes.items()}
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        deliver_to_join(arrived, **{**kwargs, "cohort_inbound_edges": wrong_routes})
    obligation = arrived.joins[0].obligations[0]
    assert obligation.delivery is not None
    assert obligation.delivery.payload is None
    assert obligation.arrival_command_fingerprint is not None


def test_direct_token_arrival_replay_validates_complete_command() -> None:
    start = _fanout(2)
    routes = _routes(start)
    token_id = start.queue[0].token_id
    command = dict(
        token_id=token_id,
        target_node_id="join",
        inbound_edge_id=routes[token_id],
        cohort_inbound_edges=routes,
        payload={"value": 1},
    )
    arrived = deliver_to_join(start, **command)
    assert deliver_to_join(arrived, **command) is arrived
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        deliver_to_join(arrived, **{**command, "failure_mode": "best_effort"})
    wrong_routes = {source_id: f"wrong-{edge_id}" for source_id, edge_id in routes.items()}
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        deliver_to_join(arrived, **{**command, "cohort_inbound_edges": wrong_routes})


def test_terminal_direct_token_arrival_replay_uses_durable_command() -> None:
    start = _fanout(2)
    routes = _routes(start)
    first_id, second_id = (child.token_id for child in start.forks[0].children)
    partial = settle_join_without_delivery(
        start,
        token_id=first_id,
        target_node_id="join",
        inbound_edge_id=routes[first_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.SUPPRESSED,
    )
    command = dict(
        token_id=second_id,
        target_node_id="join",
        inbound_edge_id=routes[second_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.SUPPRESSED,
    )
    closed = settle_join_without_delivery(partial, **command)
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert settle_join_without_delivery(closed, **command) is closed
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        settle_join_without_delivery(closed, **{**command, "failure_mode": "best_effort"})


def test_consumed_dispatch_replay_is_idempotent_when_cohort_shares_one_edge() -> None:
    start = _fanout(2)
    routes = {child.token_id: "same-edge" for child in start.forks[0].children}
    commands: list[dict[str, object]] = []
    snapshot = start
    for value in (1, 2):
        claim = claim_next_token(snapshot)
        command = {
            "dispatch_id": claim.dispatch.dispatch_id,
            "attempt": 0,
            "cancellation_generation": 0,
            "target_node_id": "join",
            "inbound_edge_id": "same-edge",
            "cohort_inbound_edges": routes,
            "payload": {"value": value},
        }
        commands.append(command)
        snapshot = deliver_to_join(claim.snapshot, **command)

    snapshot = close_ready_join(snapshot, snapshot.joins[0].join_instance_id, JoinConfig())
    assert deliver_to_join(snapshot, **commands[0]) is snapshot
    assert deliver_to_join(snapshot, **commands[1]) is snapshot
    for changed in (
        {"target_node_id": "other-target"},
        {"inbound_edge_id": "other-edge"},
        {"attempt": 1},
        {"cancellation_generation": 1},
    ):
        with pytest.raises(TokenJoinTransitionError, match="contradicts"):
            deliver_to_join(snapshot, **{**commands[0], **changed})
    with pytest.raises(TokenJoinTransitionError, match="not in flight"):
        deliver_to_join(snapshot, **{**commands[0], "dispatch_id": "different-dispatch"})


def test_arrivals_keep_sources_waiting_until_atomic_close() -> None:
    snapshot = _deliver_head(_fanout(2), payload={"left": 1})
    waiting = snapshot.joins[0].obligations[0].source_token_id
    assert (
        next(token for token in snapshot.tokens if token.token_id == waiting).scheduling_state
        is SchedulingState.JOIN_WAITING
    )

    ready = _deliver_head(snapshot, payload={"right": 2})
    closed = close_ready_join(ready, ready.joins[0].join_instance_id, JoinConfig())
    join = closed.joins[0]
    assert join.lifecycle_state is JoinLifecycleState.CLOSED
    assert join.consumed_parent_token_ids == tuple(
        item.source_token_id for item in join.obligations
    )
    assert all(
        next(token for token in closed.tokens if token.token_id == token_id).scheduling_state
        is SchedulingState.SETTLED
        for token_id in join.consumed_parent_token_ids
    )
    continuation = next(
        token for token in closed.tokens if token.token_id == join.continuation_token_id
    )
    assert continuation.continuation_parent_token_ids == join.consumed_parent_token_ids
    assert continuation.current_node_id == "join"


def test_canonical_reducer_input_preserves_edge_labels_across_arrival_orders() -> None:
    start = _fanout(3)
    routes = _routes(start)
    expected_ids = tuple(child.token_id for child in start.forks[0].children)

    # Resolve queued children deliberately out of scheduling order.
    order = (2, 0, 1)
    snapshot = start
    for ordinal in order:
        token_id = expected_ids[ordinal]
        snapshot = deliver_to_join(
            snapshot,
            token_id=token_id,
            target_node_id="join",
            inbound_edge_id=routes[token_id],
            cohort_inbound_edges=routes,
            payload={"ordinal": ordinal},
        )

    seen: list[tuple[JoinReducerInput, ...]] = []

    def reducer(config: JoinConfig, inputs: tuple[JoinReducerInput, ...]) -> object:
        seen.append(inputs)
        return reduce_join_inputs(config, inputs)

    closed = close_ready_join(
        snapshot, snapshot.joins[0].join_instance_id, JoinConfig(), reducer=reducer
    )
    assert [item.source_token_id for item in seen[0]] == list(expected_ids)
    assert [item.inbound_edge_id for item in seen[0]] == [routes[item] for item in expected_ids]
    continuation = next(
        token for token in closed.tokens if token.token_id == closed.joins[0].continuation_token_id
    )
    assert continuation.model_dump(mode="json")["payload"] == {
        "result": [
            {"ordinal": 0},
            {"ordinal": 1},
            {"ordinal": 2},
        ]
    }


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (JoinConfig(merge_strategy="collect"), {"result": [{"a": 1}, {"b": 2}]}),
        (JoinConfig(merge_strategy="merge"), {"a": 1, "b": 2}),
        (JoinConfig(merge_strategy="reduce"), {"b": 2}),
        (
            JoinConfig(
                merge_strategy="custom",
                reducer_ref="tests._fixtures.reducers.sum_scores",
            ),
            {"total": 3},
        ),
    ],
)
def test_all_reducer_strategies_share_one_deterministic_seam(
    config: JoinConfig, expected: object
) -> None:
    start = _fanout(2)
    payloads = (
        ({"total": 1}, {"total": 2}) if config.merge_strategy == "custom" else ({"a": 1}, {"b": 2})
    )
    ready = _deliver_head(_deliver_head(start, payload=payloads[0]), payload=payloads[1])
    closed = close_ready_join(ready, ready.joins[0].join_instance_id, config)
    continuation = next(
        token for token in closed.tokens if token.token_id == closed.joins[0].continuation_token_id
    )
    assert continuation.model_dump(mode="json")["payload"] == expected


@pytest.mark.parametrize(
    ("config", "payloads", "expected"),
    [
        (
            JoinConfig(merge_strategy="collect"),
            ({"a": 1}, {"b": 2}),
            {"result": [{"a": 1}, {"b": 2}]},
        ),
        (
            JoinConfig(merge_strategy="collect", merge_path="joined.items"),
            ({"a": 1}, {"b": 2}),
            {"joined": {"items": [{"a": 1}, {"b": 2}]}},
        ),
        (
            JoinConfig(merge_strategy="merge", merge_path="ignored"),
            ({"a": 1}, {"b": 2}),
            {"a": 1, "b": 2},
        ),
        (
            JoinConfig(merge_strategy="reduce", merge_path="value"),
            (1, 2),
            {"value": 2},
        ),
        (
            JoinConfig(
                merge_strategy="custom",
                reducer_ref="tests._fixtures.reducers.sum_ints",
            ),
            (1, 2),
            {"result": 3},
        ),
        (
            JoinConfig(
                merge_strategy="custom",
                reducer_ref="tests._fixtures.reducers.sum_scores",
                merge_path="ignored",
            ),
            ({"total": 1}, {"total": 2}),
            {"total": 3},
        ),
    ],
)
def test_join_config_result_shaping_matches_established_driver(
    config: JoinConfig, payloads: tuple[object, object], expected: object
) -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload=payloads[0]), payload=payloads[1])
    closed = close_ready_join(ready, ready.joins[0].join_instance_id, config)
    continuation = next(
        token for token in closed.tokens if token.token_id == closed.joins[0].continuation_token_id
    )
    assert continuation.model_dump(mode="json")["payload"] == expected


def test_suppressed_obligations_do_not_fabricate_payloads() -> None:
    start = _fanout(2)
    routes = _routes(start)
    first_id, second_id = (child.token_id for child in start.forks[0].children)
    partial = settle_join_without_delivery(
        start,
        token_id=first_id,
        target_node_id="join",
        inbound_edge_id=routes[first_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.SUPPRESSED,
    )
    assert partial.joins[0].obligations[0].delivery is None
    ready = deliver_to_join(
        partial,
        token_id=second_id,
        target_node_id="join",
        inbound_edge_id=routes[second_id],
        cohort_inbound_edges=routes,
        payload={"kept": True},
    )
    closed = close_ready_join(ready, ready.joins[0].join_instance_id, JoinConfig())
    continuation = next(
        token for token in closed.tokens if token.token_id == closed.joins[0].continuation_token_id
    )
    assert continuation.model_dump(mode="json")["payload"] == {"result": [{"kept": True}]}


def test_all_suppressed_cohort_closes_without_continuation() -> None:
    snapshot = _fanout(2)
    routes = _routes(snapshot)
    for child in snapshot.forks[0].children:
        snapshot = settle_join_without_delivery(
            snapshot,
            token_id=child.token_id,
            target_node_id="join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            outcome=JoinObligationOutcome.SUPPRESSED,
        )
    assert snapshot.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert snapshot.joins[0].continuation_token_id is None
    assert snapshot.forks[0].lifecycle_state is ForkLifecycleState.CLOSED
    assert snapshot.queue == ()


def test_fail_fast_rejects_failed_ready_join_but_best_effort_delivers() -> None:
    start = _fanout(2)
    routes = _routes(start)
    first_id, second_id = (child.token_id for child in start.forks[0].children)
    partial = deliver_to_join(
        start,
        token_id=first_id,
        target_node_id="join",
        inbound_edge_id=routes[first_id],
        cohort_inbound_edges=routes,
        payload={"ok": True},
        failure_mode="best_effort",
    )
    ready = settle_join_without_delivery(
        partial,
        token_id=second_id,
        target_node_id="join",
        inbound_edge_id=routes[second_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.FAILED,
        failure_mode="best_effort",
    )
    assert (
        close_ready_join(ready, ready.joins[0].join_instance_id, JoinConfig())
        .joins[0]
        .lifecycle_state
        is JoinLifecycleState.CLOSED
    )
    with pytest.raises(TokenJoinTransitionError, match="policy contradicts"):
        close_ready_join(
            ready,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            failure_mode="fail_fast",
        )


def test_fail_fast_failure_cancels_queued_siblings_at_arrival() -> None:
    start = _fanout(3)
    routes = _routes(start)
    failed_id = start.forks[0].children[0].token_id
    stopped = settle_join_without_delivery(
        start,
        token_id=failed_id,
        target_node_id="join",
        inbound_edge_id=routes[failed_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.FAILED,
    )
    assert stopped.joins[0].failure_mode == "fail_fast"
    assert stopped.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert stopped.cancellation_fence is None
    assert stopped.queue == ()


def test_fail_fast_discards_executing_sibling_when_it_reports() -> None:
    start = _fanout(3)
    routes = _routes(start)
    claim = claim_next_token(start)
    failed_id = claim.snapshot.queue[0].token_id
    stopped = settle_join_without_delivery(
        claim.snapshot,
        token_id=failed_id,
        target_node_id="join",
        inbound_edge_id=routes[failed_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.FAILED,
    )
    assert stopped.joins[0].lifecycle_state is JoinLifecycleState.OPEN
    dispatch = stopped.in_flight_dispatches[0]
    assert dispatch.lifecycle_state is DispatchLifecycleState.EXECUTING
    command = dict(
        dispatch_id=dispatch.dispatch_id,
        attempt=dispatch.attempt,
        cancellation_generation=dispatch.cancellation_generation,
        target_node_id="join",
        inbound_edge_id=routes[dispatch.token.token_id],
        cohort_inbound_edges=routes,
        payload={"discarded": True},
    )
    closed = deliver_to_join(
        stopped,
        **command,
    )
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert closed.in_flight_dispatches == ()
    assert deliver_to_join(closed, **command) is closed


def test_join_local_fail_fast_does_not_stale_unrelated_executing_work() -> None:
    start = _fanout(3)
    parent = start.tokens[0]
    unrelated = TokenEnvelope(
        token_id="unrelated-token",
        parent_token_id=parent.token_id,
        current_node_id="unrelated-node",
        payload=None,
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.EXECUTING,
        state_revision=start.revision,
    )
    unrelated_dispatch = InFlightDispatch(
        dispatch_id="unrelated-dispatch",
        idempotency_key="unrelated-idempotency",
        token=unrelated,
        attempt=0,
        cancellation_generation=0,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=start.revision,
        updated_revision=start.revision,
    )
    with_unrelated = start.model_copy(
        update={
            "next_token_ordinal": start.next_token_ordinal + 1,
            "tokens": (*start.tokens, unrelated),
            "in_flight_dispatches": (unrelated_dispatch,),
        }
    )
    routes = _routes(start)
    failed_id = start.forks[0].children[0].token_id
    stopped = settle_join_without_delivery(
        with_unrelated,
        token_id=failed_id,
        target_node_id="join",
        inbound_edge_id=routes[failed_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.FAILED,
    )
    assert stopped.cancellation_fence is None
    assert stopped.in_flight_dispatches == (unrelated_dispatch,)


def test_closed_join_replay_is_identity_and_conflicting_config_is_loud() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    join_id = ready.joins[0].join_instance_id
    closed = close_ready_join(ready, join_id, JoinConfig())
    assert close_ready_join(closed, join_id, JoinConfig()) is closed
    with pytest.raises(TokenJoinTransitionError, match="contradicts"):
        close_ready_join(closed, join_id, JoinConfig(merge_strategy="merge"))


def test_malformed_reducer_output_is_rejected_without_mutation() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})

    def malformed(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        return object()

    with pytest.raises(TokenJoinTransitionError, match="JSON"):
        close_ready_join(ready, ready.joins[0].join_instance_id, JoinConfig(), reducer=malformed)
    assert ready.joins[0].lifecycle_state is JoinLifecycleState.READY


def test_stale_generation_arrival_is_rejected() -> None:
    start = _fanout(2)
    claim = claim_next_token(start)
    token_id = claim.dispatch.token.token_id
    with pytest.raises(TokenJoinTransitionError, match="generation"):
        deliver_to_join(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=0,
            cancellation_generation=1,
            target_node_id="join",
            inbound_edge_id=_routes(start)[token_id],
            cohort_inbound_edges=_routes(start),
            payload=None,
        )


class _CASStore:
    def __init__(self, snapshot: TokenEngineSnapshot) -> None:
        self.snapshot = snapshot
        self.failures = 1

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot:
        assert run_id == self.snapshot.run_id
        return self.snapshot

    async def compare_and_swap_token_snapshot(
        self, run_id: str, *, expected_revision: int | None, snapshot: TokenEngineSnapshot
    ) -> TokenEngineSnapshot:
        if self.failures:
            self.failures -= 1
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        assert expected_revision == self.snapshot.revision
        self.snapshot = snapshot
        return snapshot


def test_cas_retry_produces_one_logical_arrival() -> None:
    start = _fanout(2)
    routes = _routes(start)
    token_id = start.queue[0].token_id
    store = _CASStore(start)

    committed = asyncio.run(
        apply_token_transition(
            store,
            start.run_id,
            lambda current: deliver_to_join(
                current,
                token_id=token_id,
                target_node_id="join",
                inbound_edge_id=routes[token_id],
                cohort_inbound_edges=routes,
                payload={"once": True},
            ),
        )
    )
    assert len(committed.joins) == 1
    assert sum(item.outcome is not None for item in committed.joins[0].obligations) == 1


def test_concurrent_cas_arrivals_form_one_ready_join_without_payload_loss() -> None:
    start = _fanout(2)
    routes = _routes(start)
    token_ids = tuple(routes)
    store = _CASStore(start)
    store.failures = 0

    async def arrive(token_id: str, value: int) -> TokenEngineSnapshot:
        return await apply_token_transition(
            store,
            start.run_id,
            lambda current: deliver_to_join(
                current,
                token_id=token_id,
                target_node_id="join",
                inbound_edge_id=routes[token_id],
                cohort_inbound_edges=routes,
                payload={"value": value},
            ),
        )

    async def run_arrivals() -> None:
        await asyncio.gather(arrive(token_ids[0], 0), arrive(token_ids[1], 1))

    asyncio.run(run_arrivals())
    assert len(store.snapshot.joins) == 1
    assert store.snapshot.joins[0].lifecycle_state is JoinLifecycleState.READY
    assert [
        item.delivery.model_dump(mode="json")["payload"]
        for item in store.snapshot.joins[0].obligations
        if item.delivery is not None
    ] == [{"value": 0}, {"value": 1}]


def test_close_cas_retry_invokes_reducer_once() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _CASStore(ready)
    calls = 0

    def reducer(config: JoinConfig, inputs: tuple[JoinReducerInput, ...]) -> object:
        nonlocal calls
        calls += 1
        return reduce_join_inputs(config, inputs)

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            reducer=reducer,
        )
    )
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert calls == 1


def test_reducer_exception_releases_exact_claim_before_surfacing() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _CASStore(ready)
    store.failures = 0

    def broken_reducer(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        raise RuntimeError("broken reducer")

    with pytest.raises(RuntimeError, match="broken reducer"):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                reducer=broken_reducer,
                claim_owner_id="worker-a",
            )
        )
    released = store.snapshot.joins[0]
    assert released.lifecycle_state is JoinLifecycleState.READY
    assert released.reduction_attempt == 1
    assert released.reduction_claim_id is None
    assert released.reduction_claim_owner_id is None
    assert released.reduction_claim_revision is None

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            claim_owner_id="worker-b",
        )
    )
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert closed.joins[0].reduction_attempt == 2


class _SimulatedProcessCrash(BaseException):
    pass


def _crash_after_reduction_claim(ready: TokenEngineSnapshot, store: _CASStore) -> object:
    def crash(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        raise _SimulatedProcessCrash

    with pytest.raises(_SimulatedProcessCrash):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                reducer=crash,
                claim_owner_id="crashed-worker",
            )
        )
    return orchestration.JoinReductionClaim.from_join(store.snapshot.joins[0])


def test_crashed_claim_can_be_reclaimed_and_stale_owner_cannot_close() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _CASStore(ready)
    store.failures = 0
    abandoned = _crash_after_reduction_claim(ready, store)

    with pytest.raises(orchestration.JoinReductionRecoveryError):
        asyncio.run(
            orchestration.reclaim_abandoned_join_reduction_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                observed_claim=abandoned,
                new_owner_id="",
            )
        )

    reclaimed = asyncio.run(
        orchestration.reclaim_abandoned_join_reduction_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            observed_claim=abandoned,
            new_owner_id="recovery-worker",
        )
    )
    assert reclaimed.attempt == abandoned.attempt + 1
    assert reclaimed.claim_id != abandoned.claim_id
    assert reclaimed.owner_id == "recovery-worker"

    with pytest.raises(orchestration.JoinReductionClaimChangedError):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                claimed_reduction=abandoned,
            )
        )

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            claimed_reduction=reclaimed,
        )
    )
    replayed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            claimed_reduction=reclaimed,
        )
    )
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert replayed is store.snapshot
    with pytest.raises(orchestration.JoinReductionClaimChangedError):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                claimed_reduction=abandoned,
            )
        )


def _recovered_reduction() -> tuple[TokenEngineSnapshot, _CASStore, object]:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _CASStore(ready)
    store.failures = 0
    abandoned = _crash_after_reduction_claim(ready, store)
    recovered = asyncio.run(
        orchestration.reclaim_abandoned_join_reduction_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            observed_claim=abandoned,
            new_owner_id="recovery-worker",
        )
    )
    return ready, store, recovered


def test_recovered_owner_wrong_config_is_rejected_before_reducer_evaluation() -> None:
    ready, store, recovered = _recovered_reduction()
    claimed_snapshot = store.snapshot
    calls = 0

    def reducer(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        nonlocal calls
        calls += 1
        return {"ok": True}

    with pytest.raises(TokenJoinTransitionError, match="config contradicts"):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(merge_strategy="merge"),
                reducer=reducer,
                claimed_reduction=recovered,
            )
        )
    assert calls == 0
    assert store.snapshot is claimed_snapshot

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            reducer=reducer,
            claimed_reduction=recovered,
        )
    )
    assert calls == 1
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED


def test_recovered_owner_wrong_failure_policy_is_rejected_before_reducer_evaluation() -> None:
    ready, store, recovered = _recovered_reduction()
    claimed_snapshot = store.snapshot
    calls = 0

    def reducer(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        nonlocal calls
        calls += 1
        return {"ok": True}

    with pytest.raises(TokenJoinTransitionError, match="policy contradicts"):
        asyncio.run(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                reducer=reducer,
                failure_mode="best_effort",
                claimed_reduction=recovered,
            )
        )
    assert calls == 0
    assert store.snapshot is claimed_snapshot

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            reducer=reducer,
            failure_mode="fail_fast",
            claimed_reduction=recovered,
        )
    )
    assert calls == 1
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED


def test_two_reclaimers_race_and_only_one_replaces_abandoned_claim() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _YieldingCASStore(ready)
    store.failures = 0
    abandoned = _crash_after_reduction_claim(ready, store)

    async def reclaim(owner_id: str) -> object:
        return await orchestration.reclaim_abandoned_join_reduction_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            observed_claim=abandoned,
            new_owner_id=owner_id,
            max_attempts=32,
        )

    async def race() -> tuple[object, ...]:
        return await asyncio.gather(
            reclaim("recovery-a"), reclaim("recovery-b"), return_exceptions=True
        )

    results = asyncio.run(race())
    winners = [item for item in results if isinstance(item, orchestration.JoinReductionClaim)]
    losers = [
        item for item in results if isinstance(item, orchestration.JoinReductionClaimChangedError)
    ]
    assert len(winners) == 1
    assert len(losers) == 1

    closed = asyncio.run(
        close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            claimed_reduction=winners[0],
        )
    )
    assert closed.joins[0].lifecycle_state is JoinLifecycleState.CLOSED


class _YieldingCASStore(_CASStore):
    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot:
        await asyncio.sleep(0)
        return await super().get_token_snapshot(run_id)

    async def compare_and_swap_token_snapshot(
        self, run_id: str, *, expected_revision: int | None, snapshot: TokenEngineSnapshot
    ) -> TokenEngineSnapshot:
        await asyncio.sleep(0)
        if expected_revision != self.snapshot.revision:
            raise TokenSnapshotConcurrencyError(
                run_id,
                expected_revision=expected_revision,
                actual_revision=self.snapshot.revision,
            )
        self.snapshot = snapshot
        return snapshot


def test_concurrent_closers_evaluate_reducer_once() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    store = _YieldingCASStore(ready)
    store.failures = 0
    calls = 0

    def reducer(config: JoinConfig, inputs: tuple[JoinReducerInput, ...]) -> object:
        nonlocal calls
        calls += 1
        assert all(item.inbound_edge_id for item in inputs)
        return reduce_join_inputs(config, inputs)

    async def close() -> TokenEngineSnapshot:
        return await close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            reducer=reducer,
            max_attempts=32,
        )

    async def race() -> None:
        first, second = await asyncio.gather(close(), close())
        assert first.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
        assert second.joins[0].lifecycle_state is JoinLifecycleState.CLOSED

    asyncio.run(race())
    assert calls == 1


def test_two_nested_cohorts_at_same_target_never_mix_and_resume_outer_slots() -> None:
    outer = _fanout(2)
    first = claim_next_token(outer)
    nested_a = fan_out_dispatch(
        first.snapshot,
        dispatch_id=first.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=(
            FanOutBranch(node_id="a0", inbound_edge_id="a-split-0", payload=0),
            FanOutBranch(node_id="a1", inbound_edge_id="a-split-1", payload=1),
        ),
    )
    second = claim_next_token(nested_a)
    nested_b = fan_out_dispatch(
        second.snapshot,
        dispatch_id=second.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=(
            FanOutBranch(node_id="b0", inbound_edge_id="b-split-0", payload=2),
            FanOutBranch(node_id="b1", inbound_edge_id="b-split-1", payload=3),
        ),
    )
    inner_forks = nested_b.forks[1:]
    snapshot = nested_b
    for cohort, fork in enumerate(reversed(inner_forks)):
        routes = {
            child.token_id: f"shared-{cohort}-{child.creation_ordinal}" for child in fork.children
        }
        for child in reversed(fork.children):
            snapshot = deliver_to_join(
                snapshot,
                token_id=child.token_id,
                target_node_id="shared-join",
                inbound_edge_id=routes[child.token_id],
                cohort_inbound_edges=routes,
                payload={"cohort": cohort, "child": child.creation_ordinal},
            )
    assert len(snapshot.joins) == 2
    assert {join.fork_id for join in snapshot.joins} == {fork.fork_id for fork in inner_forks}

    for join in tuple(snapshot.joins):
        snapshot = close_ready_join(snapshot, join.join_instance_id, JoinConfig())
    outer_after = snapshot.forks[0]
    continuation_ids = {join.continuation_token_id for join in snapshot.joins}
    assert {child.token_id for child in outer_after.children} == continuation_ids
    assert outer_after.outstanding_child_count == 2
    assert all(item.outcome is None for item in outer_after.obligations)


def test_all_suppressed_nested_join_settles_exact_outer_slot() -> None:
    outer = _fanout(2)
    parent_claim = claim_next_token(outer)
    nested = fan_out_dispatch(
        parent_claim.snapshot,
        dispatch_id=parent_claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=(
            FanOutBranch(node_id="inner-left", inbound_edge_id="split-left", payload=1),
            FanOutBranch(node_id="inner-right", inbound_edge_id="split-right", payload=2),
        ),
    )
    inner = nested.forks[-1]
    routes = {child.token_id: f"join-{child.creation_ordinal}" for child in inner.children}
    snapshot = nested
    for child in inner.children:
        snapshot = settle_join_without_delivery(
            snapshot,
            token_id=child.token_id,
            target_node_id="inner-join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            outcome=JoinObligationOutcome.SUPPRESSED,
        )
    outer_after = snapshot.forks[0]
    settled = [item for item in outer_after.obligations if item.outcome is not None]
    assert len(settled) == 1
    assert settled[0].outcome.value == "suppressed"
    assert outer_after.outstanding_child_count == 1


def _loop_fanout(width: int = 2) -> TokenEngineSnapshot:
    owner = TokenEnvelope(
        token_id="loop-owner",
        current_node_id="before-loop",
        payload=None,
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
        payload=None,
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
        run_id="loop-join-run",
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=2,
        tokens=(owner, executing),
        loops=(loop,),
        in_flight_dispatches=(dispatch,),
    )
    return fan_out_dispatch(
        snapshot,
        dispatch_id=dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=tuple(
            FanOutBranch(
                node_id=f"branch-{index}",
                inbound_edge_id=f"split-{index}",
                payload=index,
            )
            for index in range(width)
        ),
    )


def test_join_close_inside_iteration_transfers_frame_ownership_to_continuation() -> None:
    snapshot = _loop_fanout()
    routes = _routes(snapshot)
    for child in snapshot.forks[0].children:
        snapshot = deliver_to_join(
            snapshot,
            token_id=child.token_id,
            target_node_id="inside-join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            payload={"child": child.creation_ordinal},
        )
    closed = close_ready_join(snapshot, snapshot.joins[0].join_instance_id, JoinConfig())
    continuation_id = closed.joins[0].continuation_token_id
    assert closed.loops[0].live_child_token_ids == (continuation_id,)
    members = {member.token_id: member for member in closed.loops[0].frames[0].members}
    assert members[continuation_id].state is IterationMemberState.ACTIVE
    assert all(
        members[child.token_id].state is IterationMemberState.INTERNAL_COMPLETION
        for child in closed.forks[0].children
    )


def _ready_loop_join() -> TokenEngineSnapshot:
    snapshot = _loop_fanout()
    routes = _routes(snapshot)
    for child in snapshot.forks[0].children:
        snapshot = deliver_to_join(
            snapshot,
            token_id=child.token_id,
            target_node_id="inside-join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            payload={"child": child.creation_ordinal},
        )
    return snapshot


def test_join_persists_exact_iteration_ownership_scope() -> None:
    ready = _ready_loop_join()
    join = ready.joins[0]
    sources = [
        next(token for token in ready.tokens if token.token_id == obligation.source_token_id)
        for obligation in join.obligations
    ]
    assert all(source.provenance_tag == join.provenance_tag for source in sources)
    assert all(source.iteration_memberships == join.iteration_memberships for source in sources)


def test_snapshot_rejects_join_whose_sources_have_different_provenance() -> None:
    data = _ready_loop_join().model_dump()
    data["joins"][0]["provenance_tag"] = ()
    data["joins"][0]["iteration_memberships"] = ()
    with pytest.raises(ValueError, match="provenance"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_whose_sources_have_different_iteration_chain() -> None:
    data = _ready_loop_join().model_dump()
    membership = data["joins"][0]["iteration_memberships"][0]
    membership["loop_instance_id"] = "foreign-loop"
    membership["iteration_frame_id"] = "foreign-frame"
    with pytest.raises(ValueError, match="iteration membership"):
        TokenEngineSnapshot.model_validate(data)


def test_arrival_rejects_fork_cohort_crossing_iteration_scope() -> None:
    snapshot = _loop_fanout()
    routes = _routes(snapshot)
    first, foreign = snapshot.forks[0].children
    original = next(token for token in snapshot.tokens if token.token_id == foreign.token_id)
    foreign_membership = original.iteration_memberships[0].model_copy(
        update={"iteration_frame_id": "foreign-frame", "iteration_index": 1}
    )
    foreign_token = original.model_copy(
        update={
            "provenance_tag": (
                ProvenanceFrame(loop_header_node_id="loop-header", iteration_index=1),
            ),
            "iteration_memberships": (foreign_membership,),
        }
    )
    unsafe = snapshot.model_copy(
        update={
            "tokens": tuple(
                foreign_token if token.token_id == foreign.token_id else token
                for token in snapshot.tokens
            ),
            "queue": tuple(
                foreign_token if token.token_id == foreign.token_id else token
                for token in snapshot.queue
            ),
        }
    )
    with pytest.raises(TokenJoinTransitionError, match="iteration scope"):
        deliver_to_join(
            unsafe,
            token_id=first.token_id,
            target_node_id="inside-join",
            inbound_edge_id=routes[first.token_id],
            cohort_inbound_edges=routes,
            payload={"value": 1},
        )


def test_mixed_delivered_suppressed_join_transfers_iteration_ownership_once() -> None:
    snapshot = _loop_fanout()
    routes = _routes(snapshot)
    left, right = snapshot.forks[0].children
    snapshot = settle_join_without_delivery(
        snapshot,
        token_id=left.token_id,
        target_node_id="inside-join",
        inbound_edge_id=routes[left.token_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.SUPPRESSED,
    )
    snapshot = deliver_to_join(
        snapshot,
        token_id=right.token_id,
        target_node_id="inside-join",
        inbound_edge_id=routes[right.token_id],
        cohort_inbound_edges=routes,
        payload={"kept": True},
    )
    closed = close_ready_join(snapshot, snapshot.joins[0].join_instance_id, JoinConfig())
    continuation_id = closed.joins[0].continuation_token_id
    assert closed.loops[0].live_child_token_ids == (continuation_id,)
    assert (
        sum(
            member.token_id == continuation_id and member.state is IterationMemberState.ACTIVE
            for member in closed.loops[0].frames[0].members
        )
        == 1
    )


def test_loop_fail_fast_cancels_queued_member_while_join_remains_ready() -> None:
    snapshot = _loop_fanout(3)
    routes = _routes(snapshot)
    delivered, failed, cancelled = snapshot.forks[0].children
    snapshot = deliver_to_join(
        snapshot,
        token_id=delivered.token_id,
        target_node_id="inside-join",
        inbound_edge_id=routes[delivered.token_id],
        cohort_inbound_edges=routes,
        payload={"kept": True},
    )
    ready = settle_join_without_delivery(
        snapshot,
        token_id=failed.token_id,
        target_node_id="inside-join",
        inbound_edge_id=routes[failed.token_id],
        cohort_inbound_edges=routes,
        outcome=JoinObligationOutcome.FAILED,
    )
    assert ready.joins[0].lifecycle_state is JoinLifecycleState.READY
    members = {member.token_id: member for member in ready.loops[0].frames[0].members}
    assert members[cancelled.token_id].state is IterationMemberState.CANCELLED
    assert cancelled.token_id not in ready.loops[0].live_child_token_ids


def test_all_suppressed_join_settles_loop_members_without_continuation() -> None:
    snapshot = _loop_fanout()
    routes = _routes(snapshot)
    for child in snapshot.forks[0].children:
        snapshot = settle_join_without_delivery(
            snapshot,
            token_id=child.token_id,
            target_node_id="inside-join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            outcome=JoinObligationOutcome.SUPPRESSED,
        )
    assert snapshot.joins[0].lifecycle_state is JoinLifecycleState.CLOSED
    assert snapshot.joins[0].continuation_token_id is None
    assert snapshot.loops[0].live_child_token_ids == ()
    assert snapshot.loops[0].frames[0].state is IterationFrameState.BARRIER_READY


def _nested_loop_fanout() -> TokenEngineSnapshot:
    owner = TokenEnvelope(
        token_id="nested-owner",
        current_node_id="before-loops",
        payload=None,
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        state_revision=0,
        settled_revision=0,
    )
    outer_membership = IterationMembership(
        loop_instance_id="outer-loop",
        iteration_frame_id="outer-frame",
        loop_header_node_id="a-outer-header",
        iteration_index=0,
    )
    inner_membership = IterationMembership(
        loop_instance_id="inner-loop",
        parent_loop_instance_id="outer-loop",
        iteration_frame_id="inner-frame",
        loop_header_node_id="b-inner-header",
        iteration_index=0,
    )
    inner_owner = TokenEnvelope(
        token_id="inner-owner",
        parent_token_id=owner.token_id,
        provenance_tag=(ProvenanceFrame(loop_header_node_id="a-outer-header", iteration_index=0),),
        current_node_id="inner-header",
        payload=None,
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        iteration_memberships=(outer_membership,),
        state_revision=0,
        settled_revision=0,
    )
    executing = TokenEnvelope(
        token_id="nested-child",
        parent_token_id=inner_owner.token_id,
        provenance_tag=(
            ProvenanceFrame(loop_header_node_id="a-outer-header", iteration_index=0),
            ProvenanceFrame(loop_header_node_id="b-inner-header", iteration_index=0),
        ),
        current_node_id="inside-nested-loop",
        payload=None,
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.EXECUTING,
        iteration_memberships=(outer_membership, inner_membership),
        state_revision=0,
    )
    dispatch = InFlightDispatch(
        dispatch_id="nested-loop-dispatch",
        idempotency_key="nested-loop-idempotency",
        token=executing,
        attempt=0,
        cancellation_generation=0,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=0,
        updated_revision=0,
    )

    def frame(frame_id: str, loop_id: str) -> IterationFrame:
        return IterationFrame(
            iteration_frame_id=frame_id,
            loop_instance_id=loop_id,
            iteration_index=0,
            members=(
                IterationMember(token_id=executing.token_id, state=IterationMemberState.ACTIVE),
            ),
            state=IterationFrameState.ACTIVE,
            created_revision=0,
            updated_revision=0,
        )

    outer_frame = frame("outer-frame", "outer-loop").model_copy(
        update={
            "members": (
                IterationMember(
                    token_id=inner_owner.token_id,
                    state=IterationMemberState.INTERNAL_COMPLETION,
                    settled_revision=0,
                ),
                IterationMember(token_id=executing.token_id, state=IterationMemberState.ACTIVE),
            )
        }
    )
    outer = LoopInstance(
        loop_instance_id="outer-loop",
        loop_header_node_id="a-outer-header",
        enclosing_owner=LoopEnclosingOwner(token_id=owner.token_id),
        frames=(outer_frame,),
        live_child_token_ids=(executing.token_id,),
        next_token_ordinal=2,
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=0,
        updated_revision=0,
    )
    inner = LoopInstance(
        loop_instance_id="inner-loop",
        loop_header_node_id="b-inner-header",
        enclosing_owner=LoopEnclosingOwner(
            token_id=inner_owner.token_id,
            enclosing_loop_instance_id="outer-loop",
            iteration_frame_id="outer-frame",
        ),
        outer_provenance_tag=(
            ProvenanceFrame(loop_header_node_id="a-outer-header", iteration_index=0),
        ),
        frames=(frame("inner-frame", "inner-loop"),),
        live_child_token_ids=(executing.token_id,),
        next_token_ordinal=1,
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=0,
        updated_revision=0,
    )
    snapshot = TokenEngineSnapshot(
        run_id="nested-loop-join-run",
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=3,
        tokens=(owner, inner_owner, executing),
        loops=(outer, inner),
        in_flight_dispatches=(dispatch,),
    )
    return fan_out_dispatch(
        snapshot,
        dispatch_id=dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="split-left", payload=1),
            FanOutBranch(node_id="right", inbound_edge_id="split-right", payload=2),
        ),
    )


def test_nested_loop_join_transfers_every_membership_without_orphans() -> None:
    snapshot = _nested_loop_fanout()
    routes = _routes(snapshot)
    for child in snapshot.forks[0].children:
        snapshot = deliver_to_join(
            snapshot,
            token_id=child.token_id,
            target_node_id="nested-join",
            inbound_edge_id=routes[child.token_id],
            cohort_inbound_edges=routes,
            payload=child.creation_ordinal,
        )
    closed = close_ready_join(snapshot, snapshot.joins[0].join_instance_id, JoinConfig())
    continuation_id = closed.joins[0].continuation_token_id
    continuation = next(token for token in closed.tokens if token.token_id == continuation_id)
    assert len(continuation.iteration_memberships) == 2
    for loop in closed.loops:
        assert loop.live_child_token_ids == (continuation_id,)
        members = {member.token_id: member for member in loop.frames[0].members}
        assert members[continuation_id].state is IterationMemberState.ACTIVE
        assert all(
            members[child.token_id].state is IterationMemberState.INTERNAL_COMPLETION
            for child in closed.forks[0].children
        )


def test_ready_and_closed_snapshots_round_trip_without_reducing_on_replay() -> None:
    ready = _deliver_head(_deliver_head(_fanout(2), payload={"a": 1}), payload={"b": 2})
    restored_ready = TokenEngineSnapshot.model_validate_json(ready.model_dump_json())
    closed = close_ready_join(
        restored_ready, restored_ready.joins[0].join_instance_id, JoinConfig()
    )
    restored_closed = TokenEngineSnapshot.model_validate_json(closed.model_dump_json())

    def should_not_run(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> object:
        raise AssertionError("closed replay executed reducer")

    assert (
        close_ready_join(
            restored_closed,
            restored_closed.joins[0].join_instance_id,
            JoinConfig(),
            reducer=should_not_run,
        )
        is restored_closed
    )


def test_runtime_join_exports_are_lazy_and_cold_import_safe() -> None:
    root = Path(__file__).resolve().parents[3]
    code = """
import sys
import zeroth.runtime.orchestration as orchestration
assert 'zeroth.runtime.orchestration.driver' not in sys.modules
assert orchestration.deliver_to_join.__module__.endswith('token_joins')
assert 'zeroth.runtime.orchestration.driver' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
