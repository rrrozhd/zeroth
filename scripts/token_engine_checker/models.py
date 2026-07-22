"""Finite grammar values and validity classification."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

GRAMMAR_VERSION = "grammar-v1"
PAYLOAD_JSON = ("null", "false", "0", '{"p":1}')
REDUCERS = ("collect", "merge", "last")
RETRIES = ("none", "fail-first")
CHECKPOINTS = ("none", "before-claim", "after-claim", "after-resolve", "before-dispatch")
CANCELLATIONS = ("none", "after-cut")


@dataclass(frozen=True, slots=True)
class Classification:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Edge:
    edge_id: str
    source: str
    target: str
    parallel_ordinal: int
    condition: str | None = None
    condition_value: bool = True


def _index(label: str) -> int:
    return int(label[1:])


def canonicalize_edges(pairs: Iterable[tuple[str, str]]) -> tuple[Edge, ...]:
    """Sort edge instances and assign stable IDs and parallel ordinals."""
    counts: Counter[tuple[str, str]] = Counter()
    result: list[Edge] = []
    for index, (source, target) in enumerate(
        sorted(pairs, key=lambda pair: (_index(pair[0]), _index(pair[1])))
    ):
        pair = (source, target)
        ordinal = counts[pair]
        counts[pair] += 1
        result.append(Edge(f"e{index}", source, target, ordinal))
    return tuple(result)


def condition_edges(edges: tuple[Edge, ...]) -> tuple[Edge, ...]:
    outgoing: dict[str, list[Edge]] = defaultdict(list)
    for edge in edges:
        outgoing[edge.source].append(edge)
    conditioned: list[Edge] = []
    for edge in edges:
        group = outgoing[edge.source]
        if len(group) < 2:
            conditioned.append(edge)
            continue
        ordinal = group.index(edge)
        conditioned.append(
            Edge(
                edge.edge_id,
                edge.source,
                edge.target,
                edge.parallel_ordinal,
                f"c{_index(edge.source)}",
                ordinal % 2 == 0,
            )
        )
    return tuple(conditioned)


@dataclass(frozen=True, slots=True)
class Topology:
    nodes: tuple[str, ...]
    edges: tuple[Edge, ...]

    @property
    def condition_names(self) -> tuple[str, ...]:
        return tuple(sorted({edge.condition for edge in self.edges if edge.condition is not None}))

    @property
    def digest(self) -> str:
        material = [(edge.source, edge.target, edge.parallel_ordinal) for edge in self.edges]
        return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class State:
    payload_json: str
    reducer: str
    retry: str
    checkpoint: str
    cancellation: str

    @property
    def payload(self) -> object:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class Case:
    topology: Topology
    enabled: tuple[bool, ...]
    conditions: tuple[tuple[str, bool], ...]
    state: State

    @property
    def digest(self) -> str:
        value = {
            "topology": self.topology.digest,
            "enabled": self.enabled,
            "conditions": self.conditions,
            "state": (
                self.state.payload_json,
                self.state.reducer,
                self.state.retry,
                self.state.checkpoint,
                self.state.cancellation,
            ),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]

    def to_json(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "nodes": list(self.topology.nodes),
            "edges": [
                {
                    "id": edge.edge_id,
                    "source": edge.source,
                    "target": edge.target,
                    "parallel_ordinal": edge.parallel_ordinal,
                    "condition": edge.condition,
                    "condition_value": edge.condition_value,
                    "enabled": self.enabled[index],
                }
                for index, edge in enumerate(self.topology.edges)
            ],
            "conditions": dict(self.conditions),
            "state": {
                "payload": self.state.payload,
                "reducer": self.state.reducer,
                "retry": self.state.retry,
                "checkpoint": self.state.checkpoint,
                "cancellation": self.state.cancellation,
            },
        }


def _reachable(topology: Topology, enabled: tuple[bool, ...] | None = None) -> set[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for index, edge in enumerate(topology.edges):
        if enabled is None or enabled[index]:
            adjacency[edge.source].append(edge.target)
    seen = {topology.nodes[0]}
    pending = [topology.nodes[0]]
    while pending:
        for target in adjacency[pending.pop()]:
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def _irreducible(topology: Topology) -> bool:
    """Return whether any cyclic SCC has more than one external entry node."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for edge in topology.edges:
        adjacency[edge.source].append(edge.target)
        reverse[edge.target].append(edge.source)

    order: list[str] = []
    seen: set[str] = set()

    def visit(node: str) -> None:
        if node in seen:
            return
        seen.add(node)
        for target in adjacency[node]:
            visit(target)
        order.append(node)

    for node in topology.nodes:
        visit(node)
    seen.clear()

    def collect(node: str, component: set[str]) -> None:
        if node in seen:
            return
        seen.add(node)
        component.add(node)
        for source in reverse[node]:
            collect(source, component)

    for node in reversed(order):
        if node in seen:
            continue
        component: set[str] = set()
        collect(node, component)
        cyclic = len(component) > 1 or any(
            edge.source == edge.target and edge.source in component for edge in topology.edges
        )
        if not cyclic:
            continue
        entries = {
            edge.target
            for edge in topology.edges
            if edge.source not in component and edge.target in component
        }
        if len(entries) > 1:
            return True
    return False


def classify_topology(topology: Topology) -> Classification:
    expected_nodes = tuple(f"n{index}" for index in range(len(topology.nodes)))
    if topology.nodes != expected_nodes or len(topology.nodes) < 2:
        return Classification(False, "node_labels")
    if len(topology.edges) > len(topology.nodes) + 1:
        return Classification(False, "edge_bound")
    pairs = Counter((edge.source, edge.target) for edge in topology.edges)
    if any(count > 2 for count in pairs.values()):
        return Classification(False, "parallel_edge_bound")
    if any(edge.source == edge.target for edge in topology.edges):
        return Classification(False, "self_edge")
    if any(edge.source == topology.nodes[-1] for edge in topology.edges):
        return Classification(False, "terminal_not_sink")
    out_degree = Counter(edge.source for edge in topology.edges)
    if any(count > 3 for count in out_degree.values()):
        return Classification(False, "fanout_width")
    if _reachable(topology) != set(topology.nodes):
        return Classification(False, "unreachable_node")
    if _irreducible(topology):
        return Classification(False, "irreducible")
    if len(topology.condition_names) > 2:
        return Classification(False, "condition_bound")
    return Classification(True)


def classify_case(case: Case) -> Classification:
    topology_result = classify_topology(case.topology)
    if not topology_result.valid:
        return topology_result
    if len(case.enabled) != len(case.topology.edges):
        return Classification(False, "enabled_mask_length")
    if case.topology.nodes[-1] not in _reachable(case.topology, case.enabled):
        return Classification(False, "no_enabled_terminal_path")
    expected_conditions = case.topology.condition_names
    if tuple(name for name, _ in case.conditions) != expected_conditions:
        return Classification(False, "condition_valuation")
    if case.state.payload_json not in PAYLOAD_JSON:
        return Classification(False, "unknown_payload")
    if case.state.reducer not in REDUCERS:
        return Classification(False, "unknown_reducer")
    if case.state.retry not in RETRIES:
        return Classification(False, "unknown_retry")
    if case.state.checkpoint not in CHECKPOINTS:
        return Classification(False, "unknown_checkpoint")
    if case.state.cancellation not in CANCELLATIONS:
        return Classification(False, "unknown_cancellation")
    if case.state.cancellation == "after-cut" and case.state.checkpoint == "none":
        return Classification(False, "cancellation_without_checkpoint")
    return Classification(True)
