from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest
import zeroth.runtime.orchestration.token_runtime_support as runtime_support

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import (
    IterationMemberState,
    JoinLifecycleState,
    PayloadDelivery,
)
from zeroth.runtime.orchestration.token_lifecycle import (
    pause_snapshot,
    request_cancellation,
    stop_snapshot,
)
from zeroth.runtime.orchestration.token_loop_claims import _claim_loop_with_cas
from zeroth.runtime.orchestration.token_runtime import TokenRuntimeCoordinator
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    claim_next_token,
    fan_out_dispatch,
    initialize_token_snapshot,
)
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.orchestration.token_joins import (
    JoinReductionClaim,
    close_ready_join,
    close_ready_join_with_cas,
    deliver_to_join,
)
from zeroth.runtime.orchestration.token_loops import enter_loop, settle_loop_member
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
        self.orchestrator = None

    async def external_stop(self, run: Run) -> Run:
        self.stops += 1
        return run

    @staticmethod
    def _merge_join_payloads(_graph, _target_node_id, payloads):
        return payloads


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


def test_graceful_stop_claims_already_owned_fork_work() -> None:
    root = initialize_token_snapshot(run_id="run-stop-fork", root_node_id="root", payload={})
    parent = claim_next_token(root)
    forked = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="left", inbound_edge_id="root-left", payload={}),
            FanOutBranch(node_id="right", inbound_edge_id="root-right", payload={}),
        ),
    )
    stopping = stop_snapshot(forked)

    claim = claim_next_token(stopping)

    assert claim.dispatch.token.token_id == forked.queue[0].token_id
    assert claim.snapshot.state is TokenEngineSnapshotState.STOPPING


async def test_graceful_stop_recovers_and_drains_persisted_join_claim() -> None:
    root = initialize_token_snapshot(run_id="run-join-claim", root_node_id="root", payload={})
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
        child.token_id: f"join-{child.creation_ordinal}" for child in snapshot.forks[0].children
    }
    for payload in ("left", "right"):
        claim = claim_next_token(snapshot)
        snapshot = deliver_to_join(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=claim.dispatch.attempt,
            cancellation_generation=claim.dispatch.cancellation_generation,
            target_node_id="join",
            inbound_edge_id=routes[claim.dispatch.token.token_id],
            cohort_inbound_edges=routes,
            payload=payload,
        )
    store = _MemoryStore(snapshot)

    class _Crash(BaseException):
        pass

    def crash_reducer(_config, _inputs):
        raise _Crash

    with pytest.raises(_Crash):
        await close_ready_join_with_cas(
            store,
            snapshot.run_id,
            snapshot.joins[0].join_instance_id,
            JoinConfig(),
            reducer=crash_reducer,
            claim_owner_id="crashed-worker",
        )
    store.snapshot = stop_snapshot(store.snapshot)
    coordinator = TokenRuntimeCoordinator(_Driver(), store)
    edge = SimpleNamespace(edge_id=next(iter(routes.values())), target_node_id="join")
    node = SimpleNamespace(node_id="join", join_config=JoinConfig())
    graph = SimpleNamespace(
        edges=(edge,),
        nodes=(node,),
        execution_settings=SimpleNamespace(failure_policy="fail_fast"),
    )

    drained = await coordinator._drain_stopping_owner(
        graph, _run(snapshot.run_id, RunStatus.WAITING_INTERRUPT), store.snapshot
    )

    assert drained.joins[0].lifecycle_state is JoinLifecycleState.CLOSED


async def test_deferred_join_merge_reloads_waiters_after_cas_race() -> None:
    root = initialize_token_snapshot(run_id="run-deferred-race", root_node_id="A", payload={})
    parent = claim_next_token(root)
    forked = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="B", inbound_edge_id="A-B", payload={}),
            FanOutBranch(node_id="C", inbound_edge_id="A-C", payload={}),
        ),
    )
    left = claim_next_token(forked)
    deferred = TokenRuntimeCoordinator._append_deferred_join_delivery(
        left.snapshot,
        target_node_id="T",
        inbound_edge_id="B-T",
        payload={"source": "B"},
        dispatch_id=left.dispatch.dispatch_id,
        attempt=left.dispatch.attempt,
        cancellation_generation=left.dispatch.cancellation_generation,
    )
    routes = {child.token_id: f"{child.token_id}-J" for child in forked.forks[0].children}
    waiting = deliver_to_join(
        deferred,
        dispatch_id=left.dispatch.dispatch_id,
        attempt=left.dispatch.attempt,
        cancellation_generation=left.dispatch.cancellation_generation,
        target_node_id="J",
        inbound_edge_id=routes[left.dispatch.token.token_id],
        cohort_inbound_edges=routes,
        payload={"source": "B"},
    )
    right = claim_next_token(waiting)
    first = right.snapshot.deferred_join_deliveries[0]
    raced_delivery = first.model_copy(
        update={
            "delivery_id": "delivery_raced",
            "source_token_id": "token_raced",
            "inbound_edge_id": "D-T",
            "delivery": PayloadDelivery(payload={"source": "D"}),
            "created_revision": right.snapshot.revision + 1,
        }
    )
    raced = right.snapshot.model_copy(
        update={
            "revision": right.snapshot.revision + 1,
            "deferred_join_deliveries": (
                *right.snapshot.deferred_join_deliveries,
                raced_delivery,
            ),
        }
    )

    class _ConflictOnceStore(_MemoryStore):
        conflicted = False

        async def compare_and_swap_token_snapshot(self, run_id, *, expected_revision, snapshot):
            if not self.conflicted:
                self.conflicted = True
                self.snapshot = raced
                raise TokenSnapshotConcurrencyError(
                    run_id,
                    expected_revision=expected_revision,
                    actual_revision=raced.revision,
                )
            return await super().compare_and_swap_token_snapshot(
                run_id, expected_revision=expected_revision, snapshot=snapshot
            )

    store = _ConflictOnceStore(right.snapshot)
    coordinator = TokenRuntimeCoordinator(_Driver(), store)

    committed = await coordinator._transition(
        right.snapshot,
        partial(
            coordinator._close_deferred_join,
            dispatch_id=right.dispatch.dispatch_id,
            attempt=right.dispatch.attempt,
            cancellation_generation=right.dispatch.cancellation_generation,
            target_node_id="T",
            inbound_edge_id="C-T",
            current_delivery=PayloadDelivery(payload={"source": "C"}),
            edge_order={"B-T": 0, "D-T": 1, "C-T": 2},
            merge_payloads=lambda payloads: payloads,
        ),
    )

    assert committed.queue[-1].model_dump(mode="json")["payload"] == [
        {"source": "B"},
        {"source": "D"},
        {"source": "C"},
    ]
    assert committed.deferred_join_deliveries == ()


async def test_graceful_stop_recovers_and_drains_persisted_loop_claim() -> None:
    root = initialize_token_snapshot(run_id="run-loop-claim", root_node_id="header", payload={})
    entered = enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="header",
        body_node_id="body",
        inbound_edge_id="header-body",
        exit_routes={"body-exit": "done"},
    )
    ready = settle_loop_member(
        entered,
        token_id=entered.queue[0].token_id,
        outcome=IterationMemberState.INTERNAL_COMPLETION,
    )
    store = _MemoryStore(ready)
    await _claim_loop_with_cas(
        store,
        ready.run_id,
        ready.loops[0].loop_instance_id,
        JoinConfig(),
        owner_id="crashed-worker",
        max_attempts=2,
    )
    store.snapshot = stop_snapshot(store.snapshot)
    coordinator = TokenRuntimeCoordinator(_Driver(), store)
    graph = SimpleNamespace(
        edges=(),
        nodes=(SimpleNamespace(node_id="header", join_config=JoinConfig()),),
    )

    drained = await coordinator._drain_stopping_owner(
        graph, _run(ready.run_id, RunStatus.WAITING_INTERRUPT), store.snapshot
    )

    assert drained.loops[0].reduction_claim_id is None
    assert drained.revision > store.snapshot.revision - 1


async def test_join_claim_replay_resolves_already_claimed_continuation_from_tokens(
    monkeypatch,
) -> None:
    root = initialize_token_snapshot(run_id="run-join-race", root_node_id="root", payload={})
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
        child.token_id: f"join-{child.creation_ordinal}" for child in snapshot.forks[0].children
    }
    for payload in ("left", "right"):
        claim = claim_next_token(snapshot)
        snapshot = deliver_to_join(
            claim.snapshot,
            dispatch_id=claim.dispatch.dispatch_id,
            attempt=claim.dispatch.attempt,
            cancellation_generation=claim.dispatch.cancellation_generation,
            target_node_id="join",
            inbound_edge_id=routes[claim.dispatch.token.token_id],
            cohort_inbound_edges=routes,
            payload=payload,
        )
    store = _MemoryStore(snapshot)

    class _Crash(BaseException):
        pass

    def crash_reducer(_config, _inputs):
        raise _Crash

    with pytest.raises(_Crash):
        await close_ready_join_with_cas(
            store,
            snapshot.run_id,
            snapshot.joins[0].join_instance_id,
            JoinConfig(),
            reducer=crash_reducer,
            claim_owner_id="crashed-worker",
        )
    stale_reducing = store.snapshot
    closed = close_ready_join(
        stale_reducing,
        stale_reducing.joins[0].join_instance_id,
        JoinConfig(),
        claimed_reduction=JoinReductionClaim.from_join(stale_reducing.joins[0]),
    )
    raced = claim_next_token(closed).snapshot
    assert raced.queue == ()

    async def replayed_close(*_args, **_kwargs):
        return raced

    monkeypatch.setattr(runtime_support, "close_ready_join_with_cas", replayed_close)
    coordinator = TokenRuntimeCoordinator(_Driver(), store)
    edge = SimpleNamespace(edge_id=next(iter(routes.values())), target_node_id="join")
    graph = SimpleNamespace(
        edges=(edge,),
        nodes=(SimpleNamespace(node_id="join", join_config=JoinConfig()),),
        execution_settings=SimpleNamespace(failure_policy="fail_fast"),
    )

    result = await coordinator._close_join_if_ready(
        graph,
        _run(snapshot.run_id, RunStatus.WAITING_INTERRUPT),
        stale_reducing,
        edge,
        (),
    )

    assert result is raced
