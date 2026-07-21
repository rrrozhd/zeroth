"""Adapter from grammar cases to production token pure transitions."""

from __future__ import annotations

import asyncio
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    IterationMemberState,
    JoinLifecycleState,
    JoinObligationOutcome,
)
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.orchestration.token_joins import (
    close_ready_join,
    deliver_to_join,
    settle_join_without_delivery,
)
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
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap.migrations import run_migrations

from .models import Case, Edge, State, classify_case
from .oracle import Dispatch, Resolution, Trace


class UnsupportedValidCaseError(RuntimeError):
    """The production adapter rejected a case classified as valid."""


def _active(edge: Edge, enabled: bool, conditions: dict[str, bool]) -> bool:
    return enabled and (
        edge.condition is None or conditions[edge.condition] is edge.condition_value
    )


def _structured_descriptors(
    case: Case,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, str | None], ...]]:
    conditions = dict(case.conditions)
    active = tuple(
        edge
        for index, edge in enumerate(case.topology.edges)
        if _active(edge, case.enabled[index], conditions)
    )
    join_cohorts = tuple(
        edge_ids
        for target in case.topology.nodes
        if len(
            edge_ids := tuple(
                sorted(edge.edge_id for edge in active if edge.target == target)
            )
        )
        >= 2
    )
    back_edges = sorted(
        (edge for edge in active if int(edge.target[1:]) <= int(edge.source[1:])),
        key=lambda edge: edge.edge_id,
    )
    loop_routes: list[tuple[str, str | None]] = []
    for back in back_edges:
        low = int(back.target[1:])
        high = int(back.source[1:])
        exits = tuple(
            edge
            for edge in active
            if edge.edge_id != back.edge_id
            and low <= int(edge.source[1:]) <= high
            and not low <= int(edge.target[1:]) <= high
            and int(edge.target[1:]) > int(edge.source[1:])
        )
        loop_routes.extend(
            (back.edge_id, edge.edge_id) for edge in sorted(exits, key=lambda item: item.edge_id)
        )
        if not exits:
            loop_routes.append((back.edge_id, None))
    return join_cohorts, tuple(loop_routes)


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


def _cancel_all(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    cancelled = request_cancellation(snapshot)
    fence = cancelled.cancellation_fence
    if fence is None:
        return cancelled
    for dispatch in tuple(cancelled.in_flight_dispatches):
        cancelled = acknowledge_cancellation(
            cancelled,
            dispatch_id=dispatch.dispatch_id,
            cancellation_generation=fence.generation,
        )
    return cancelled


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


@cache
def _repository_probe() -> dict[str, object]:
    """Exercise the production SQL repository's reload and stale-CAS fence."""

    async def exercise(path: Path) -> dict[str, object]:
        database = AsyncSQLiteDatabase(path=str(path))
        repository = RunRepository(database)
        try:
            run = Run(
                run_id="checker-repository-probe",
                workflow_name="token-checker",
                graph_version_ref="checker:v1",
                deployment_ref="checker",
                tenant_id="checker",
            )
            await repository.create(run)
            initial = initialize_token_snapshot(
                run_id=run.run_id,
                root_node_id="probe-root",
                payload=None,
            )
            await repository.compare_and_swap_token_snapshot(
                run.run_id, expected_revision=None, snapshot=initial
            )
            reloaded = await repository.get_token_snapshot(run.run_id)
            assert reloaded is not None
            claimed = claim_next_token(reloaded).snapshot
            await repository.compare_and_swap_token_snapshot(
                run.run_id,
                expected_revision=reloaded.revision,
                snapshot=claimed,
            )
            conflict_fenced = False
            try:
                await repository.compare_and_swap_token_snapshot(
                    run.run_id,
                    expected_revision=reloaded.revision,
                    snapshot=claimed,
                )
            except TokenSnapshotConcurrencyError:
                conflict_fenced = True
            final = await repository.get_token_snapshot(run.run_id)
            assert final is not None
            return {
                "implementation": "RunRepository",
                "cas_writes": 2,
                "reloads": 2,
                "conflict_fenced": conflict_fenced,
                "final_revision": final.revision,
            }
        finally:
            await database.close()

    with tempfile.TemporaryDirectory(prefix="zeroth-checker-repository-") as directory:
        path = Path(directory) / "checker.db"
        run_migrations(f"sqlite:///{path}")
        return asyncio.run(exercise(path))


def _join_probe(
    case: _ProbeInput, edge_ids: tuple[str, ...], mutation: str | None
) -> tuple[dict[str, object], _SnapshotRepository] | None:
    if len(edge_ids) < 2:
        return None
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
            branches=tuple(
                FanOutBranch(
                    node_id=f"probe-branch-{index}",
                    inbound_edge_id=f"probe-split-{edge_id}",
                    payload={"branch": edge_id},
                )
                for index, edge_id in enumerate(edge_ids)
            ),
        )
    )
    fork = repository.load().forks[0]
    routes = {
        child.token_id: edge_ids[child.creation_ordinal] for child in fork.children
    }
    failure_mode = "best_effort" if case.state.retry == "fail-first" else "fail_fast"
    if mutation == "failure_policy_globalized":
        failure_mode = "fail_fast" if failure_mode == "best_effort" else "best_effort"
    for index, edge_id in enumerate(edge_ids):
        child_box = []

        def claim_child(
            snapshot: TokenEngineSnapshot, box: list = child_box
        ) -> TokenEngineSnapshot:
            claim = claim_next_token(snapshot)
            box.append(claim.dispatch)
            return claim.snapshot

        repository.apply(claim_child)
        child = child_box.pop()
        if index == 0:
            repository.apply(
                lambda snapshot, child=child: settle_join_without_delivery(
                    snapshot,
                    dispatch_id=child.dispatch_id,
                    attempt=child.attempt,
                    cancellation_generation=child.cancellation_generation,
                    target_node_id="probe-join",
                    inbound_edge_id=routes[child.token.token_id],
                    cohort_inbound_edges=routes,
                    outcome=JoinObligationOutcome.FAILED,
                    failure_mode=failure_mode,
                )
            )
            if failure_mode == "fail_fast":
                break
        else:
            repository.apply(
                lambda snapshot, child=child, edge_id=edge_id: deliver_to_join(
                    snapshot,
                    dispatch_id=child.dispatch_id,
                    attempt=child.attempt,
                    cancellation_generation=child.cancellation_generation,
                    target_node_id="probe-join",
                    inbound_edge_id=routes[child.token.token_id],
                    cohort_inbound_edges=routes,
                    payload={"edge": edge_id},
                    failure_mode=failure_mode,
                )
            )
    ready = repository.load().joins[0]

    def reducer(_config, inputs):
        return _reduce(
            case.state.reducer,
            [(item.inbound_edge_id, _plain(item.payload)) for item in inputs],
        )

    if ready.lifecycle_state is JoinLifecycleState.READY:
        closed = repository.apply(
            lambda snapshot: close_ready_join(
                snapshot,
                ready.join_instance_id,
                JoinConfig(),
                reducer=reducer,
                failure_mode=ready.failure_mode,
            )
        )
        if mutation == "join_closes_twice":
            repository.apply(
                lambda snapshot: close_ready_join(
                    snapshot,
                    ready.join_instance_id,
                    JoinConfig(),
                    reducer=reducer,
                    failure_mode=ready.failure_mode,
                )
            )
        join = closed.joins[0]
    else:
        join = ready
    return (
        {
            "state": join.lifecycle_state.value,
            "continuation_created": join.continuation_token_id is not None,
            "failure_policy": join.failure_mode,
            "edge_ids": list(edge_ids),
            "obligation_outcomes": [item.outcome.value for item in join.obligations],
        },
        repository,
    )


def _loop_probe(
    case: _ProbeInput,
    back_edge_id: str | None,
    exit_edge_id: str | None,
    mutation: str | None,
) -> tuple[dict[str, object], _SnapshotRepository] | None:
    if back_edge_id is None:
        return None
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
            exit_routes=({exit_edge_id: "probe-done"} if exit_edge_id is not None else {}),
        )
    )
    member_id = entered.queue[0].token_id
    first_ready = repository.apply(
        lambda snapshot: settle_loop_member(
            snapshot,
            token_id=member_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id=back_edge_id,
            payload=case.state.payload,
        )
    )
    continued = repository.apply(
        lambda snapshot: close_ready_loop(
            snapshot,
            first_ready.loops[0].loop_instance_id,
            continuation_config=JoinConfig(),
        )
    )
    second_member_id = continued.queue[0].token_id
    if exit_edge_id is None:
        final_ready = repository.apply(
            lambda snapshot: settle_loop_member(
                snapshot,
                token_id=second_member_id,
                outcome=IterationMemberState.INTERNAL_COMPLETION,
                payload=case.state.payload,
            )
        )
    else:
        final_ready = repository.apply(
            lambda snapshot: settle_loop_member(
                snapshot,
                token_id=second_member_id,
                outcome=IterationMemberState.EXIT_DELIVERY,
                edge_id=exit_edge_id,
                target_node_id="probe-done",
                payload=case.state.payload,
            )
        )
    if mutation == "loop_owner_leaks":
        completed = final_ready
    else:
        completed = repository.apply(
            lambda snapshot: close_ready_loop(
                snapshot,
                final_ready.loops[0].loop_instance_id,
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
            "back_edge_id": back_edge_id,
            "frames": [frame.state.value for frame in loop.frames],
        },
        repository,
    )


def _lifecycle_probe(
    case: _ProbeInput, mutation: str | None
) -> tuple[dict[str, object], _SnapshotRepository]:
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
                cancellation_generation=(
                    0
                    if mutation == "cancellation_generation_lost"
                    else requested.cancellation_fence.generation
                ),
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
    join_cohorts: tuple[tuple[str, ...], ...],
    loop_routes: tuple[tuple[str, str | None], ...],
    mutation: str | None,
) -> dict[str, object]:
    probe = _ProbeInput(State(payload_json, reducer, retry, checkpoint, cancellation))
    join_results = tuple(
        result
        for edge_ids in join_cohorts
        if (result := _join_probe(probe, edge_ids, mutation)) is not None
    )
    loop_results = tuple(
        result
        for back_edge_id, exit_edge_id in loop_routes
        if (
            result := _loop_probe(
                probe, back_edge_id, exit_edge_id, mutation
            )
        )
        is not None
    )
    lifecycle, _lifecycle_repository = _lifecycle_probe(probe, mutation)
    joins = tuple(result[0] for result in join_results)
    loops = tuple(result[0] for result in loop_results)
    join = joins[0] if joins else {
        "state": "not_applicable",
        "continuation_created": False,
        "failure_policy": "best_effort" if retry == "fail-first" else "fail_fast",
        "edge_ids": [],
        "obligation_outcomes": [],
    }
    loop = loops[0] if loops else {
        "state": "not_applicable",
        "resolved_exit_edges": [],
        "frames": [],
        "back_edge_id": None,
    }
    return {
        "join": join,
        "joins": joins,
        "loop": loop,
        "loops": loops,
        "lifecycle": lifecycle,
        "repository": _repository_probe(),
    }


class ProductionAdapter:
    """Drive only production functions whose input and output are snapshots."""

    def __init__(self, *, mutation: str | None = None) -> None:
        self.mutation = mutation

    def run(self, case: Case, schedule: tuple[str, ...] | None = None) -> Trace:
        classification = classify_case(case)
        if not classification.valid:
            raise ValueError(f"invalid case: {classification.reason}")
        try:
            effective_schedule = None if self.mutation == "schedule_input_discarded" else schedule
            return self._run(case, schedule=effective_schedule)
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
        checkpoint_reloads = 0
        graph_cancelled = False
        schedule_rank = {
            token_id: index for index, token_id in enumerate(schedule or ())
        }
        pending = (
            tuple(logical_by_actual[token.token_id] for token in snapshot.queue)
            if self.mutation == "retain_pending"
            else ()
        )

        while snapshot.queue and not pending:
            if not checkpoint_done and case.state.checkpoint == "before-claim":
                if self.mutation != "checkpoint_reload_skipped":
                    snapshot = _round_trip(snapshot)
                    checkpoint_reloads += 1
                checkpoint_done = True
                if case.state.cancellation == "after-cut":
                    snapshot = _cancel_all(snapshot)
                    lifecycle.append(("snapshot", "cancelled-before-claim"))
                    graph_cancelled = True
                    break
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
            cancel_after_claim = False
            if not checkpoint_done and case.state.checkpoint in {
                "after-claim",
                "before-dispatch",
            }:
                if self.mutation != "checkpoint_reload_skipped":
                    snapshot = _round_trip(snapshot)
                    checkpoint_reloads += 1
                checkpoint_done = True
                cancel_after_claim = case.state.cancellation == "after-cut"
            dispatches.append(
                Dispatch(
                    dispatch.token.current_node_id,
                    logical_id,
                    dispatch.attempt,
                    dispatch.token.causal_inbound_edge_id,
                    _plain(dispatch.token.payload),
                )
            )
            if self.mutation == "duplicate_dispatch":
                dispatches.append(dispatches[-1])
            if cancel_after_claim:
                snapshot = _cancel_all(snapshot)
                lifecycle.append((logical_id, "cancelled-after-claim"))
                graph_cancelled = True
                break
            if case.state.retry == "fail-first":
                if self.mutation != "retry_lifecycle_lost":
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
                if self.mutation != "persisted_terminal_dropped":
                    terminal_payload = _plain(dispatch.token.payload)
                    if self.mutation == "terminal_output_corrupted":
                        terminal_payload = {"corrupted_terminal": True}
                    terminals.append((logical_id, terminal_payload))
                snapshot = complete_dispatch(
                    snapshot,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                )
                if not checkpoint_done and case.state.checkpoint == "after-resolve":
                    if self.mutation != "checkpoint_reload_skipped":
                        snapshot = _round_trip(snapshot)
                        checkpoint_reloads += 1
                    checkpoint_done = True
                    if case.state.cancellation == "after-cut":
                        snapshot = _cancel_all(snapshot)
                        lifecycle.append((logical_id, "cancelled-after-resolve"))
                        graph_cancelled = True
                        break
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
                if self.mutation == "corrupt_payload":
                    payload = {"corrupted": True}
                resolution = Resolution(
                    edge.edge_id,
                    logical_id,
                    edge.source,
                    edge.target,
                    is_delivered,
                    payload,
                )
                if self.mutation != "drop_resolution" or resolutions:
                    resolutions.append(resolution)
                if self.mutation == "duplicate_resolution" and len(resolutions) == 1:
                    resolutions.append(resolution)
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
                if self.mutation != "checkpoint_reload_skipped":
                    snapshot = _round_trip(snapshot)
                    checkpoint_reloads += 1
                checkpoint_done = True
                if case.state.cancellation == "after-cut":
                    snapshot = _cancel_all(snapshot)
                    lifecycle.append((logical_id, "cancelled-after-resolve"))
                    graph_cancelled = True
                    break

        join_cohorts, loop_routes = _structured_descriptors(case)
        if not graph_cancelled and not snapshot.queue:
            snapshot = stop_snapshot(snapshot)
        production = _production_probe(
            case.state.payload_json,
            case.state.reducer,
            case.state.retry,
            case.state.checkpoint,
            case.state.cancellation,
            join_cohorts,
            loop_routes,
            self.mutation,
        )
        lifecycle.extend(
            (
                ("structured-join", str(production["join"]["state"])),
                ("structured-loop", str(production["loop"]["state"])),
                ("snapshot", str(production["lifecycle"]["state"])),
            )
        )
        production = {
            **production,
            "graph_execution": {
                "state": snapshot.state.value,
                "cancelled": graph_cancelled,
                "pending_token_ids": sorted(
                    logical_by_actual[token.token_id] for token in snapshot.queue
                ),
                "dispatch_count": len(dispatches),
                "checkpoint_reloads": checkpoint_reloads,
            },
        }
        return Trace(
            case.digest,
            tuple(resolutions),
            tuple(dispatches),
            _reduce(case.state.reducer, terminals),
            pending=pending,
            lifecycle=tuple(lifecycle),
            persisted_state={
                "terminal": terminals,
                "checkpoint": case.state.checkpoint,
                "dispatch_order": [item.token_id for item in dispatches],
                "production": production,
            },
        )
