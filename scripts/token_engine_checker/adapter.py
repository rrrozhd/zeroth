"""Adapter from grammar cases to production token pure transitions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    claim_next_token,
    complete_dispatch,
    enqueue_dispatch,
    fan_out_dispatch,
    initialize_token_snapshot,
    retry_dispatch,
)

from .models import Case, Edge, classify_case
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


class ProductionAdapter:
    """Drive only production functions whose input and output are snapshots."""

    def run(self, case: Case, schedule: tuple[str, ...] | None = None) -> Trace:
        del schedule  # Production claims its own canonical durable queue order.
        classification = classify_case(case)
        if not classification.valid:
            raise ValueError(f"invalid case: {classification.reason}")
        try:
            return self._run(case)
        except Exception as error:
            if isinstance(error, UnsupportedValidCaseError):
                raise
            raise UnsupportedValidCaseError(str(error)) from error

    def _run(self, case: Case) -> Trace:
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

        while snapshot.queue:
            if not checkpoint_done and case.state.checkpoint in {"before-claim", "before-dispatch"}:
                snapshot = _round_trip(snapshot)
                checkpoint_done = True
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

        return Trace(
            case.digest,
            tuple(resolutions),
            tuple(dispatches),
            _reduce(case.state.reducer, terminals),
            lifecycle=tuple(lifecycle),
            persisted_state={"terminal": terminals, "checkpoint": case.state.checkpoint},
        )
