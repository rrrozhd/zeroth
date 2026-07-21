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


class Oracle:
    """Execute grammar cases using only the appendix's abstract semantics."""

    def run(self, case: Case, schedule: tuple[str, ...] | None = None) -> Trace:
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
        steps = 0
        while queue:
            if schedule:
                rank = {token_id: index for index, token_id in enumerate(schedule)}
                queue.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0]))
            token_id, node, payload, inbound, back_counts = queue.pop(0)
            attempts = 2 if case.state.retry == "fail-first" else 1
            for attempt in range(attempts):
                dispatches.append(Dispatch(node, token_id, attempt, inbound, payload))
                lifecycle.append((token_id, "retry" if attempt + 1 < attempts else "complete"))
            if node == case.topology.nodes[-1]:
                terminals.append((token_id, payload))
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
            steps += 1
            if steps > 10_000:
                raise OracleViolation("bounded grammar exceeded transition limit")
        trace = Trace(
            case.digest,
            tuple(resolutions),
            tuple(dispatches),
            _reduce(case.state.reducer, terminals),
            lifecycle=tuple(lifecycle),
            persisted_state={"terminal": terminals, "checkpoint": case.state.checkpoint},
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
