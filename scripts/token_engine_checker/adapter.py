"""Adapter from grammar cases to production token pure transitions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import IterationMemberState
from zeroth.runtime.orchestration.token_joins import close_ready_join, deliver_to_join
from zeroth.runtime.orchestration.token_lifecycle import (
    acknowledge_cancellation,
    pause_snapshot,
    request_cancellation,
    resume_snapshot,
    stop_snapshot,
)
from zeroth.runtime.orchestration.token_loops import (
    close_ready_loop,
    enter_loop,
    settle_loop_member,
)
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    claim_next_token,
    complete_dispatch,
    enqueue_dispatch,
    fan_out_dispatch,
    initialize_token_snapshot,
    retry_dispatch,
)

from .models import Case, Edge, State, classify_case
from .oracle import Dispatch, Resolution, Trace


class UnsupportedValidCaseError(RuntimeError):
    """The production adapter rejected a case classified as valid."""


def _active(edge: Edge, enabled: bool, conditions: dict[str, bool]) -> bool:
    return enabled and (
        edge.condition is None or conditions[edge.condition] is edge.condition_value
    )


def _reduce(reducer: str, values: list[tuple[str, object]]) -> object:
    ordered = [value for _, value in sorted(values)]
    if reducer == "collect":
        value: object = ordered
    elif reducer == "last":
        value = ordered[-1] if ordered else None
    else:
        merged: dict[str, object] = {}
        scalars: list[object] = []
        for item in ordered:
            if isinstance(item, dict):
                merged.update(item)
            else:
                scalars.append(item)
        if scalars:
            merged["values"] = scalars
        value = merged
    return {"reducer": reducer, "value": value}


def _round_trip(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    return TokenEngineSnapshot.model_validate_json(snapshot.model_dump_json())


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class _SnapshotRepository:
    """Serialized CAS/reload boundary used by the production checker adapter."""

    def __init__(self, snapshot: TokenEngineSnapshot) -> None:
        self._encoded = snapshot.model_dump_json()
        self.cas_writes = 1
        self.reloads = 0

    def load(self) -> TokenEngineSnapshot:
        self.reloads += 1
        return TokenEngineSnapshot.model_validate_json(self._encoded)

    def apply(
        self, transition: Callable[[TokenEngineSnapshot], TokenEngineSnapshot]
    ) -> TokenEngineSnapshot:
        current = self.load()
        proposed = transition(current)
        if proposed.revision <= current.revision:
            raise UnsupportedValidCaseError("repository CAS did not advance revision")
        self._encoded = proposed.model_dump_json()
        self.cas_writes += 1
        return proposed


@dataclass(frozen=True, slots=True)
class _ProbeInput:
    state: State


def _join_probe(case: _ProbeInput) -> tuple[dict[str, object], _SnapshotRepository]:
    repository = _SnapshotRepository(
        initialize_token_snapshot(
            run_id=f"checker-join-{case.state.reducer}-{case.state.retry}",
            root_node_id="probe-root",
            payload=case.state.payload,
        )
    )
    claim_box = []

    def claim_root(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
        claim = claim_next_token(snapshot)
        claim_box.append(claim.dispatch)
        return claim.snapshot

    repository.apply(claim_root)
    parent = claim_box.pop()
    repository.apply(
        lambda snapshot: fan_out_dispatch(
            snapshot,
            dispatch_id=parent.dispatch_id,
            attempt=parent.attempt,
            cancellation_generation=parent.cancellation_generation,
            branches=(
                FanOutBranch(
                    node_id="probe-left",
                    inbound_edge_id="probe-split-left",
                    payload={"branch": "left"},
                ),
                FanOutBranch(
                    node_id="probe-right",
                    inbound_edge_id="probe-split-right",
                    payload={"branch": "right"},
                ),
            ),
        )
    )
    fork = repository.load().forks[0]
    routes = {
        child.token_id: f"probe-join-{child.creation_ordinal}" for child in fork.children
    }
    for payload in ({"left": 1}, {"right": 2}):
        child_box = []

        def claim_child(
            snapshot: TokenEngineSnapshot, box: list = child_box
        ) -> TokenEngineSnapshot:
            claim = claim_next_token(snapshot)
            box.append(claim.dispatch)
            return claim.snapshot

        repository.apply(claim_child)
        child = child_box.pop()
        repository.apply(
            lambda snapshot, child=child, payload=payload: deliver_to_join(
                snapshot,
                dispatch_id=child.dispatch_id,
                attempt=child.attempt,
                cancellation_generation=child.cancellation_generation,
                target_node_id="probe-join",
                inbound_edge_id=routes[child.token.token_id],
                cohort_inbound_edges=routes,
                payload=payload,
                failure_mode=(
                    "best_effort" if case.state.retry == "fail-first" else "fail_fast"
                ),
            )
        )
    ready = repository.load().joins[0]

    def reducer(_config, inputs):
        return _reduce(
            case.state.reducer,
            [(item.inbound_edge_id, _plain(item.payload)) for item in inputs],
        )

    closed = repository.apply(
        lambda snapshot: close_ready_join(
            snapshot,
            ready.join_instance_id,
            JoinConfig(),
            reducer=reducer,
            failure_mode=ready.failure_mode,
        )
    )
    join = closed.joins[0]
    return (
        {
            "state": join.lifecycle_state.value,
            "continuation_created": join.continuation_token_id is not None,
            "failure_policy": join.failure_mode,
            "obligation_outcomes": [item.outcome.value for item in join.obligations],
        },
        repository,
    )


def _loop_probe(case: _ProbeInput) -> tuple[dict[str, object], _SnapshotRepository]:
    repository = _SnapshotRepository(
        initialize_token_snapshot(
            run_id=f"checker-loop-{case.state.payload_json}",
            root_node_id="probe-header",
            payload=case.state.payload,
        )
    )
    entered = repository.apply(
        lambda snapshot: enter_loop(
            snapshot,
            token_id=snapshot.tokens[0].token_id,
            loop_header_node_id="probe-header",
            body_node_id="probe-body",
            inbound_edge_id="probe-body-edge",
            exit_routes={"probe-exit": "probe-done"},
        )
    )
    member_id = entered.queue[0].token_id
    ready = repository.apply(
        lambda snapshot: settle_loop_member(
            snapshot,
            token_id=member_id,
            outcome=IterationMemberState.EXIT_DELIVERY,
            edge_id="probe-exit",
            target_node_id="probe-done",
            payload=case.state.payload,
        )
    )
    completed = repository.apply(
        lambda snapshot: close_ready_loop(
            snapshot,
            ready.loops[0].loop_instance_id,
            continuation_config=JoinConfig(),
        )
    )
    loop = completed.loops[0]
    return (
        {
            "state": loop.lifecycle_state.value,
            "resolved_exit_edges": [
                exit_state.exit_edge_id
                for exit_state in loop.exits
                if exit_state.resolution_outcome is not None
            ],
            "frames": [frame.state.value for frame in loop.frames],
        },
        repository,
    )


def _lifecycle_probe(case: _ProbeInput) -> tuple[dict[str, object], _SnapshotRepository]:
    repository = _SnapshotRepository(
        initialize_token_snapshot(
            run_id=f"checker-life-{case.state.checkpoint}-{case.state.cancellation}",
            root_node_id="probe-work",
            payload=case.state.payload,
        )
    )
    dispatch_box = []

    def claim_work(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
        claim = claim_next_token(snapshot)
        dispatch_box.append(claim.dispatch)
        return claim.snapshot

    repository.apply(claim_work)
    dispatch = dispatch_box.pop()
    if case.state.cancellation == "after-cut":
        requested = repository.apply(request_cancellation)
        assert requested.cancellation_fence is not None
        final = repository.apply(
            lambda snapshot: acknowledge_cancellation(
                snapshot,
                dispatch_id=dispatch.dispatch_id,
                cancellation_generation=requested.cancellation_fence.generation,
            )
        )
    else:
        repository.apply(
            lambda snapshot: complete_dispatch(
                snapshot,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
            )
        )
        repository.apply(pause_snapshot)
        repository.apply(resume_snapshot)
        final = repository.apply(stop_snapshot)
    return (
        {
            "state": final.state.value,
            "checkpoint": case.state.checkpoint,
            "cancellation_generation": (
                final.cancellation_fence.generation
                if final.cancellation_fence is not None
                else 0
            ),
        },
        repository,
    )


@cache
def _production_probe(
    payload_json: str,
    reducer: str,
    retry: str,
    checkpoint: str,
    cancellation: str,
) -> dict[str, object]:
    probe = _ProbeInput(State(payload_json, reducer, retry, checkpoint, cancellation))
    join, join_repository = _join_probe(probe)
    loop, loop_repository = _loop_probe(probe)
    lifecycle, lifecycle_repository = _lifecycle_probe(probe)
    repositories = (join_repository, loop_repository, lifecycle_repository)
    return {
        "join": join,
        "loop": loop,
        "lifecycle": lifecycle,
        "repository": {
            "cas_writes": sum(item.cas_writes for item in repositories),
            "reloads": sum(item.reloads for item in repositories),
        },
    }


class ProductionAdapter:
    """Drive only production functions whose input and output are snapshots."""

    def run(self, case: Case, schedule: tuple[str, ...] | None = None) -> Trace:
        classification = classify_case(case)
        if not classification.valid:
            raise ValueError(f"invalid case: {classification.reason}")
        try:
            return self._run(case, schedule=schedule)
        except Exception as error:
            if isinstance(error, UnsupportedValidCaseError):
                raise
            raise UnsupportedValidCaseError(str(error)) from error

    def _run(self, case: Case, *, schedule: tuple[str, ...] | None) -> Trace:
        snapshot = initialize_token_snapshot(
            run_id=f"checker-{case.digest}",
            root_node_id=case.topology.nodes[0],
            payload=case.state.payload,
        )
        conditions = dict(case.conditions)
        outgoing: dict[str, list[tuple[int, Edge]]] = defaultdict(list)
        for index, edge in enumerate(case.topology.edges):
            outgoing[edge.source].append((index, edge))
        logical_by_actual = {snapshot.queue[0].token_id: "t0"}
        counts_by_actual: dict[str, dict[str, int]] = {snapshot.queue[0].token_id: {}}
        resolutions: list[Resolution] = []
        dispatches: list[Dispatch] = []
        lifecycle: list[tuple[str, str]] = []
        terminals: list[tuple[str, object]] = []
        checkpoint_done = False
        schedule_rank = {
            token_id: index for index, token_id in enumerate(schedule or ())
        }

        while snapshot.queue:
            if not checkpoint_done and case.state.checkpoint in {"before-claim", "before-dispatch"}:
                snapshot = _round_trip(snapshot)
                checkpoint_done = True
            if schedule_rank:
                snapshot = snapshot.model_copy(
                    update={
                        "queue": tuple(
                            sorted(
                                snapshot.queue,
                                key=lambda token: (
                                    schedule_rank.get(
                                        logical_by_actual[token.token_id], len(schedule_rank)
                                    ),
                                    logical_by_actual[token.token_id],
                                ),
                            )
                        )
                    }
                )
            claim = claim_next_token(snapshot)
            snapshot = claim.snapshot
            dispatch = claim.dispatch
            actual_id = dispatch.token.token_id
            logical_id = logical_by_actual[actual_id]
            if not checkpoint_done and case.state.checkpoint == "after-claim":
                snapshot = _round_trip(snapshot)
                checkpoint_done = True
            dispatches.append(
                Dispatch(
                    dispatch.token.current_node_id,
                    logical_id,
                    dispatch.attempt,
                    dispatch.token.causal_inbound_edge_id,
                    _plain(dispatch.token.payload),
                )
            )
            if case.state.retry == "fail-first":
                lifecycle.append((logical_id, "retry"))
                retried = retry_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                )
                snapshot = retried.snapshot
                dispatch = retried.dispatch
                dispatches.append(
                    Dispatch(
                        dispatch.token.current_node_id,
                        logical_id,
                        dispatch.attempt,
                        dispatch.token.causal_inbound_edge_id,
                        _plain(dispatch.token.payload),
                    )
                )
            lifecycle.append((logical_id, "complete"))
            node = dispatch.token.current_node_id
            if node == case.topology.nodes[-1]:
                terminals.append((logical_id, _plain(dispatch.token.payload)))
                snapshot = complete_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                )
                continue

            active_edges = [
                edge
                for index, edge in outgoing[node]
                if _active(edge, case.enabled[index], conditions)
            ]
            delivered: list[tuple[Edge, str, object, dict[str, int]]] = []
            back_counts = counts_by_actual[actual_id]
            for edge in active_edges:
                is_back = int(edge.target[1:]) <= int(edge.source[1:])
                traversals = back_counts.get(edge.edge_id, 0)
                is_delivered = not is_back or traversals < 2
                child_id = f"{logical_id}.{edge.edge_id}.{traversals}"
                payload = {
                    "edge": edge.edge_id,
                    "token": child_id,
                    "value": _plain(dispatch.token.payload),
                }
                resolutions.append(
                    Resolution(
                        edge.edge_id,
                        logical_id,
                        edge.source,
                        edge.target,
                        is_delivered,
                        payload,
                    )
                )
                if is_delivered:
                    child_counts = dict(back_counts)
                    if is_back:
                        child_counts[edge.edge_id] = traversals + 1
                    delivered.append((edge, child_id, payload, child_counts))

            if not delivered:
                snapshot = complete_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                )
            elif len(delivered) == 1:
                edge, child_id, payload, child_counts = delivered[0]
                snapshot = enqueue_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                    next_node_id=edge.target,
                    inbound_edge_id=edge.edge_id,
                    payload=payload,
                )
                logical_by_actual[actual_id] = child_id
                counts_by_actual[actual_id] = child_counts
            else:
                known_ids = set(logical_by_actual)
                snapshot = fan_out_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                    branches=tuple(
                        FanOutBranch(
                            node_id=edge.target,
                            inbound_edge_id=edge.edge_id,
                            payload=payload,
                        )
                        for edge, _child_id, payload, _counts in delivered
                    ),
                )
                children = [token for token in snapshot.queue if token.token_id not in known_ids]
                if len(children) != len(delivered):
                    raise UnsupportedValidCaseError("fan-out child cardinality mismatch")
                for child, (_edge, child_id, _payload, child_counts) in zip(
                    children, delivered, strict=True
                ):
                    logical_by_actual[child.token_id] = child_id
                    counts_by_actual[child.token_id] = child_counts
            if not checkpoint_done and case.state.checkpoint == "after-resolve":
                snapshot = _round_trip(snapshot)
                checkpoint_done = True

        production = _production_probe(
            case.state.payload_json,
            case.state.reducer,
            case.state.retry,
            case.state.checkpoint,
            case.state.cancellation,
        )
        lifecycle.extend(
            (
                ("structured-join", str(production["join"]["state"])),
                ("structured-loop", str(production["loop"]["state"])),
                ("snapshot", str(production["lifecycle"]["state"])),
            )
        )
        return Trace(
            case.digest,
            tuple(resolutions),
            tuple(dispatches),
            _reduce(case.state.reducer, terminals),
            lifecycle=tuple(lifecycle),
            persisted_state={
                "terminal": terminals,
                "checkpoint": case.state.checkpoint,
                "production": production,
            },
        )
