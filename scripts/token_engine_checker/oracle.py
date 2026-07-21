"""Independent executable token oracle.  This module never imports the runtime."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace

from .models import Case, Edge, classify_case


class OracleViolation(AssertionError):  # noqa: N818 - domain term used in reports
    pass


@dataclass(frozen=True, slots=True)
class Resolution:
    edge_id: str
    token_id: str
    source: str
    target: str
    delivered: bool
    payload: object


@dataclass(frozen=True, slots=True)
class Dispatch:
    node_id: str
    token_id: str
    attempt: int
    inbound_edge_id: str | None
    payload: object


@dataclass(frozen=True, slots=True)
class Trace:
    case_digest: str
    resolutions: tuple[Resolution, ...]
    dispatches: tuple[Dispatch, ...]
    terminal_output: object
    pending: tuple[str, ...] = ()
    lifecycle: tuple[tuple[str, str], ...] = ()
    persisted_state: object = None

    def with_resolutions(self, resolutions: Iterable[Resolution]) -> Trace:
        return replace(self, resolutions=tuple(resolutions))


def _active(edge: Edge, enabled: bool, conditions: dict[str, bool]) -> bool:
    if not enabled:
        return False
    if edge.condition is None:
        return True
    return conditions[edge.condition] is edge.condition_value


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


def _abstract_production_state(case: Case) -> dict[str, object]:
    cancelled = case.state.cancellation == "after-cut"
    conditions = dict(case.conditions)
    active_edges = tuple(
        edge
        for index, edge in enumerate(case.topology.edges)
        if _active(edge, case.enabled[index], conditions)
    )
    incoming = tuple(
        sorted(
            max(
                (
                    tuple(
                        edge.edge_id
                        for edge in active_edges
                        if edge.target == target
                    )
                    for target in case.topology.nodes
                ),
                key=lambda edge_ids: (len(edge_ids), edge_ids),
                default=(),
            )
        )
    )
    best_effort = case.state.retry == "fail-first"
    back_edges = tuple(
        edge
        for edge in active_edges
        if int(edge.target[1:]) <= int(edge.source[1:])
    )
    if back_edges:
        back = min(back_edges, key=lambda edge: edge.edge_id)
        exits = tuple(
            edge
            for edge in active_edges
            if edge.edge_id != back.edge_id
            and edge.source == back.source
            and int(edge.target[1:]) > int(edge.source[1:])
        )
        if not exits:
            exits = tuple(
                edge
                for edge in active_edges
                if edge.edge_id != back.edge_id
                and int(edge.target[1:]) > int(edge.source[1:])
            )
        exit_id = min(exits, key=lambda edge: edge.edge_id).edge_id if exits else "bounded-exit"
        loop = {
            "state": "completed",
            "resolved_exit_edges": [exit_id],
            "frames": ["settled", "settled"],
            "back_edge_id": back.edge_id,
        }
    else:
        loop = {
            "state": "not_applicable",
            "resolved_exit_edges": [],
            "frames": [],
            "back_edge_id": None,
        }
    join = (
        {
            "state": "closed",
            "continuation_created": best_effort,
            "failure_policy": "best_effort" if best_effort else "fail_fast",
            "edge_ids": list(incoming),
            "obligation_outcomes": (
                ["failed", *(["delivered"] * (len(incoming) - 1))]
                if best_effort
                else ["failed", *(["cancelled"] * (len(incoming) - 1))]
            ),
        }
        if len(incoming) >= 2
        else {
            "state": "not_applicable",
            "continuation_created": False,
            "failure_policy": "best_effort" if best_effort else "fail_fast",
            "edge_ids": [],
            "obligation_outcomes": [],
        }
    )
    return {
        "join": join,
        "loop": loop,
        "lifecycle": {
            "state": "cancelled" if cancelled else "stopped",
            "checkpoint": case.state.checkpoint,
            "cancellation_generation": 1 if cancelled else 0,
        },
        "repository": {
            "implementation": "RunRepository",
            "cas_writes": 2,
            "reloads": 2,
            "conflict_fenced": True,
            "final_revision": 1,
        },
    }


class Oracle:
    """Execute grammar cases using only the appendix's abstract semantics."""

    def ready_sets(self, case: Case) -> tuple[tuple[str, ...], ...]:
        """Return canonical logical token IDs for each observed concurrent state."""
        observed: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.run(case, _ready_states=observed)
        return tuple(dict.fromkeys(ready for _prefix, ready in observed))

    def run(
        self,
        case: Case,
        schedule: tuple[str, ...] | None = None,
        *,
        _ready_states: list[tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
    ) -> Trace:
        classification = classify_case(case)
        if not classification.valid:
            raise OracleViolation(f"invalid case: {classification.reason}")
        conditions = dict(case.conditions)
        outgoing: dict[str, list[tuple[int, Edge]]] = defaultdict(list)
        for index, edge in enumerate(case.topology.edges):
            outgoing[edge.source].append((index, edge))

        queue: list[tuple[str, str, object, str | None, dict[str, int]]] = [
            ("t0", case.topology.nodes[0], case.state.payload, None, {})
        ]
        resolutions: list[Resolution] = []
        dispatches: list[Dispatch] = []
        lifecycle: list[tuple[str, str]] = []
        terminals: list[tuple[str, object]] = []
        dispatch_prefix: list[str] = []
        checkpoint_done = False
        graph_cancelled = False
        steps = 0
        while queue:
            if not checkpoint_done and case.state.checkpoint == "before-claim":
                checkpoint_done = True
                if case.state.cancellation == "after-cut":
                    queue.clear()
                    lifecycle.append(("snapshot", "cancelled-before-claim"))
                    graph_cancelled = True
                    break
            if _ready_states is not None and len(queue) > 1:
                _ready_states.append(
                    (tuple(dispatch_prefix), tuple(sorted(item[0] for item in queue)))
                )
            if schedule:
                rank = {token_id: index for index, token_id in enumerate(schedule)}
                queue.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0]))
            token_id, node, payload, inbound, back_counts = queue.pop(0)
            dispatch_prefix.append(token_id)
            cancel_after_claim = False
            if not checkpoint_done and case.state.checkpoint in {
                "after-claim",
                "before-dispatch",
            }:
                checkpoint_done = True
                cancel_after_claim = case.state.cancellation == "after-cut"
            if cancel_after_claim:
                dispatches.append(Dispatch(node, token_id, 0, inbound, payload))
                lifecycle.append((token_id, "cancelled-after-claim"))
                queue.clear()
                graph_cancelled = True
                break
            attempts = 2 if case.state.retry == "fail-first" else 1
            for attempt in range(attempts):
                dispatches.append(Dispatch(node, token_id, attempt, inbound, payload))
                lifecycle.append((token_id, "retry" if attempt + 1 < attempts else "complete"))
            if node == case.topology.nodes[-1]:
                terminals.append((token_id, payload))
                if not checkpoint_done and case.state.checkpoint == "after-resolve":
                    checkpoint_done = True
                    if case.state.cancellation == "after-cut":
                        queue.clear()
                        lifecycle.append((token_id, "cancelled-after-resolve"))
                        graph_cancelled = True
                        break
                continue
            active = [
                edge
                for index, edge in outgoing[node]
                if _active(edge, case.enabled[index], conditions)
            ]
            for edge in active:
                is_back = int(edge.target[1:]) <= int(edge.source[1:])
                traversals = back_counts.get(edge.edge_id, 0)
                delivered = not is_back or traversals < 2
                child_id = f"{token_id}.{edge.edge_id}.{traversals}"
                edge_payload = {"edge": edge.edge_id, "token": child_id, "value": payload}
                resolutions.append(
                    Resolution(
                        edge.edge_id,
                        token_id,
                        edge.source,
                        edge.target,
                        delivered,
                        edge_payload,
                    )
                )
                if delivered:
                    child_counts = dict(back_counts)
                    if is_back:
                        child_counts[edge.edge_id] = traversals + 1
                    queue.append((child_id, edge.target, edge_payload, edge.edge_id, child_counts))
            if not checkpoint_done and case.state.checkpoint == "after-resolve":
                checkpoint_done = True
                if case.state.cancellation == "after-cut":
                    queue.clear()
                    lifecycle.append((token_id, "cancelled-after-resolve"))
                    graph_cancelled = True
                    break
            steps += 1
            if steps > 10_000:
                raise OracleViolation("bounded grammar exceeded transition limit")
        production = _abstract_production_state(case)
        production = {
            **production,
            "graph_execution": {
                "state": "cancelled" if graph_cancelled else "running",
                "cancelled": graph_cancelled,
                "pending_token_ids": sorted(item[0] for item in queue),
                "dispatch_count": len(dispatches),
                "checkpoint_reloads": 0 if case.state.checkpoint == "none" else 1,
            },
        }
        lifecycle.extend(
            (
                ("structured-join", str(production["join"]["state"])),
                ("structured-loop", str(production["loop"]["state"])),
                ("snapshot", str(production["lifecycle"]["state"])),
            )
        )
        trace = Trace(
            case.digest,
            tuple(resolutions),
            tuple(dispatches),
            _reduce(case.state.reducer, terminals),
            lifecycle=tuple(lifecycle),
            persisted_state={
                "terminal": terminals,
                "checkpoint": case.state.checkpoint,
                "dispatch_order": [item.token_id for item in dispatches],
                "production": production,
            },
        )
        self.validate(trace)
        return trace

    def validate(self, trace: Trace) -> None:
        keys: set[tuple[str, str]] = set()
        for event in trace.resolutions:
            key = (event.edge_id, event.token_id)
            if key in keys:
                raise OracleViolation(f"duplicate edge resolution: {key!r}")
            keys.add(key)
            json.dumps(event.payload, allow_nan=False, sort_keys=True)
        if trace.pending:
            raise OracleViolation(f"terminal trace retains pending tokens: {trace.pending!r}")
