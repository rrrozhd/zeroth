"""Independent trace oracle for the opt-in token engine.

The helpers in this module deliberately do not import the runtime.  They model
the externally observable edge-resolution contract and are intended to consume
runtime traces in later integration tests.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from typing import Any

import pytest


TAG = (("loop", 2),)
type TokenTag = tuple[tuple[str, int], ...]


class TraceViolationError(AssertionError):
    pass


@dataclass(frozen=True)
class EdgeSpec:
    edge_id: str
    source: str
    target: str
    enabled: bool = True


@dataclass(frozen=True)
class EdgeResolution:
    edge_id: str
    tag: TokenTag
    delivered: bool
    payload_fingerprint: str | None


@dataclass(frozen=True)
class Dispatch:
    node: str
    tag: TokenTag
    payload_fingerprints: tuple[str, ...]
    actual_payload_fingerprint: str | None = None


@dataclass(frozen=True)
class TerminalState:
    pending_nodes: tuple[str, ...] = ()
    staged_payloads: tuple[tuple[str, str], ...] = ()
    staged_tags: tuple[tuple[str, TokenTag], ...] = ()
    join_buckets: tuple[tuple[str, TokenTag], ...] = ()
    join_state_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Trace:
    edges: tuple[EdgeSpec, ...]
    resolutions: tuple[EdgeResolution, ...]
    dispatches: tuple[Dispatch, ...]
    expected_activations: tuple[tuple[str, TokenTag], ...]
    terminal: TerminalState
    expected_dispatch_edges: tuple[tuple[str, TokenTag, tuple[str, ...]], ...] = ()
    expected_dispatch_payloads: tuple[tuple[str, TokenTag, str], ...] = ()


def payload_fingerprint(edge_id: str, payload: Any) -> str:
    """Return a stable edge-labelled digest, even for identical payload values."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    return f"{edge_id}:{digest}"


def assert_trace_contract(trace: Trace) -> None:
    """Validate dispatch readiness, reducer membership, and terminal cleanup."""
    edge_by_id = {edge.edge_id: edge for edge in trace.edges}
    inbound: dict[str, set[str]] = defaultdict(set)
    for edge in trace.edges:
        if edge.enabled:
            inbound[edge.target].add(edge.edge_id)

    resolved: dict[tuple[str, TokenTag], list[EdgeResolution]] = defaultdict(list)
    resolution_keys: set[tuple[str, TokenTag]] = set()
    for event in trace.resolutions:
        edge = edge_by_id.get(event.edge_id)
        if edge is None:
            raise TraceViolationError(f"resolution references unknown edge {event.edge_id}")
        if not edge.enabled and event.delivered:
            raise TraceViolationError(f"disabled edge {event.edge_id} was delivered")
        resolution_key = (event.edge_id, event.tag)
        if resolution_key in resolution_keys:
            raise TraceViolationError(
                f"duplicate edge resolution for {event.edge_id} {event.tag!r}"
            )
        resolution_keys.add(resolution_key)
        resolved[(edge.target, event.tag)].append(event)

    dispatched: dict[tuple[str, TokenTag], list[Dispatch]] = defaultdict(list)
    for event in trace.dispatches:
        dispatched[(event.node, event.tag)].append(event)

    if trace.expected_dispatch_edges:
        declared = {
            (node, tag): Counter(edge_ids) for node, tag, edge_ids in trace.expected_dispatch_edges
        }
        if len(declared) != len(trace.expected_dispatch_edges):
            raise TraceViolationError("duplicate declared dispatch edge membership")
        if declared.keys() != dispatched.keys():
            raise TraceViolationError("declared edge membership has different dispatch activations")
        for key, expected_edges in declared.items():
            events = dispatched[key]
            if len(events) != 1:
                continue
            observed_edges = Counter(
                fingerprint.rsplit(":", 1)[0] for fingerprint in events[0].payload_fingerprints
            )
            if observed_edges != expected_edges:
                raise TraceViolationError(
                    f"{key!r} declared edge membership differs: "
                    f"expected {expected_edges}, got {observed_edges}"
                )

    if trace.expected_dispatch_payloads:
        declared_payloads = {
            (node, tag): fingerprint for node, tag, fingerprint in trace.expected_dispatch_payloads
        }
        if len(declared_payloads) != len(trace.expected_dispatch_payloads):
            raise TraceViolationError("duplicate declared dispatch payload")
        if declared_payloads.keys() != dispatched.keys():
            raise TraceViolationError(
                "declared dispatch payload has different dispatch activations"
            )
        for key, expected_fingerprint in declared_payloads.items():
            events = dispatched[key]
            if len(events) != 1:
                continue
            if events[0].actual_payload_fingerprint != expected_fingerprint:
                raise TraceViolationError(
                    f"{key!r} actual dispatch payload differs: expected "
                    f"{expected_fingerprint}, got {events[0].actual_payload_fingerprint}"
                )

    expected = set(trace.expected_activations)
    if len(expected) != len(trace.expected_activations):
        raise TraceViolationError("duplicate expected node/tag activation")
    observed = resolved.keys() | dispatched.keys()
    unexpected = observed - expected
    if unexpected:
        raise TraceViolationError(f"unexpected node/tag activation: {sorted(unexpected)!r}")

    for key in expected:
        node, tag = key
        events = resolved[key]
        if not events:
            raise TraceViolationError(f"{node} {tag!r} expected activation has no resolution")
        enabled_events = [event for event in events if edge_by_id[event.edge_id].enabled]
        resolved_ids = {event.edge_id for event in enabled_events}
        required_ids = inbound[node]
        dispatch_events = dispatched[key]

        if resolved_ids != required_ids:
            missing = sorted(required_ids - resolved_ids)
            raise TraceViolationError(f"{node} {tag!r} has unresolved enabled inbound: {missing}")

        delivered = [event for event in enabled_events if event.delivered]
        should_dispatch = resolved_ids == required_ids and bool(delivered)
        if should_dispatch and len(dispatch_events) != 1:
            raise TraceViolationError(
                f"{node} {tag!r} requires exactly one dispatch; got {len(dispatch_events)}"
            )
        if not should_dispatch and dispatch_events:
            raise TraceViolationError(f"{node} {tag!r} dispatched without a delivered inbound")

        if dispatch_events:
            expected = Counter(event.payload_fingerprint for event in delivered)
            actual = Counter(dispatch_events[0].payload_fingerprints)
            if actual != expected:
                raise TraceViolationError(
                    f"{node} {tag!r} delivered payload membership differs: "
                    f"expected {expected}, got {actual}"
                )

    terminal_fields = (
        ("pending nodes", trace.terminal.pending_nodes),
        ("staged payloads", trace.terminal.staged_payloads),
        ("staged tags", trace.terminal.staged_tags),
        ("join buckets", trace.terminal.join_buckets),
        ("join state nodes", trace.terminal.join_state_nodes),
    )
    for label, value in terminal_fields:
        if value:
            raise TraceViolationError(f"terminal state retains {label}: {value!r}")


def _observable(trace: Trace) -> tuple[object, ...]:
    enabled_ids = {edge.edge_id for edge in trace.edges if edge.enabled}
    resolutions = tuple(event for event in trace.resolutions if event.edge_id in enabled_ids)
    return resolutions, trace.dispatches, trace.terminal


def assert_disabled_equals_removed(disabled_trace: Trace, removed_trace: Trace) -> None:
    """A disabled edge must be observationally identical to deleting it."""
    if _observable(disabled_trace) != _observable(removed_trace):
        raise TraceViolationError("disabled edge differs from removal")


def assert_replay_equivalent(uninterrupted: Trace, replayed: Trace) -> None:
    """Reloading a checkpoint must not alter resolution or dispatch observations."""
    if _observable(uninterrupted) != _observable(replayed):
        raise TraceViolationError("replay differs from uninterrupted execution")


def _valid_join_case():
    edges = (
        EdgeSpec("left-join", "LEFT", "JOIN"),
        EdgeSpec("right-join", "RIGHT", "JOIN"),
    )
    left = payload_fingerprint("left-join", {"value": "same-looking"})
    right = payload_fingerprint("right-join", {"value": "same-looking"})
    assert left != right
    resolutions = (
        EdgeResolution("left-join", TAG, True, left),
        EdgeResolution("right-join", TAG, True, right),
    )
    dispatches = (Dispatch("JOIN", TAG, (left, right)),)
    return edges, resolutions, dispatches


def test_oracle_accepts_complete_edge_labelled_join_trace():
    edges, resolutions, dispatches = _valid_join_case()

    assert_trace_contract(Trace(edges, resolutions, dispatches, (("JOIN", TAG),), TerminalState()))


def test_oracle_rejects_expected_activation_with_no_events():
    edges, _resolutions, _dispatches = _valid_join_case()
    empty = Trace(
        edges,
        (),
        (),
        (("JOIN", TAG),),
        TerminalState(),
    )

    with pytest.raises(TraceViolationError, match="expected activation has no resolution"):
        assert_trace_contract(empty)


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_oracle_rejects_missing_or_duplicate_dispatch_per_node_tag(mutation):
    edges, resolutions, dispatches = _valid_join_case()
    broken = () if mutation == "missing" else dispatches * 2

    with pytest.raises(TraceViolationError, match="exactly one dispatch"):
        assert_trace_contract(Trace(edges, resolutions, broken, (("JOIN", TAG),), TerminalState()))


def test_oracle_rejects_dispatch_before_every_enabled_inbound_resolves():
    edges, resolutions, dispatches = _valid_join_case()

    with pytest.raises(TraceViolationError, match="unresolved enabled inbound"):
        assert_trace_contract(
            Trace(edges, resolutions[:1], dispatches, (("JOIN", TAG),), TerminalState())
        )


def test_oracle_rejects_incomplete_inbound_resolution_without_dispatch():
    edges, resolutions, _dispatches = _valid_join_case()

    with pytest.raises(TraceViolationError, match="unresolved enabled inbound"):
        assert_trace_contract(Trace(edges, resolutions[:1], (), (("JOIN", TAG),), TerminalState()))


def test_oracle_rejects_duplicate_resolution_for_one_edge_tag():
    edges, resolutions, _dispatches = _valid_join_case()
    duplicated = (resolutions[0], resolutions[0], resolutions[1])
    payloads = tuple(event.payload_fingerprint for event in duplicated)

    with pytest.raises(TraceViolationError, match="duplicate edge resolution"):
        assert_trace_contract(
            Trace(
                edges,
                duplicated,
                (Dispatch("JOIN", TAG, payloads),),
                (("JOIN", TAG),),
                TerminalState(),
            )
        )


def test_payload_fingerprint_rejects_non_json_payloads():
    with pytest.raises(TypeError, match="JSON"):
        payload_fingerprint("edge", {"unordered"})


def test_oracle_rejects_wrong_delivered_payload_membership():
    edges, resolutions, dispatches = _valid_join_case()
    wrong = Dispatch("JOIN", TAG, (dispatches[0].payload_fingerprints[0], "invented"))

    with pytest.raises(TraceViolationError, match="delivered payload membership"):
        assert_trace_contract(
            Trace(edges, resolutions, (wrong,), (("JOIN", TAG),), TerminalState())
        )


def test_oracle_rejects_dispatch_edges_that_differ_from_independent_fixture():
    edges, resolutions, dispatches = _valid_join_case()
    trace = Trace(
        edges,
        resolutions,
        dispatches,
        (("JOIN", TAG),),
        TerminalState(),
        (("JOIN", TAG, ("left-join", "invented-edge")),),
    )

    with pytest.raises(TraceViolationError, match="declared edge membership"):
        assert_trace_contract(trace)


def test_oracle_rejects_actual_dispatch_payload_that_differs_from_fixture():
    edges, resolutions, dispatches = _valid_join_case()
    actual = Dispatch(
        "JOIN",
        TAG,
        dispatches[0].payload_fingerprints,
        payload_fingerprint("JOIN", {"corrupted": True}),
    )
    trace = Trace(
        edges,
        resolutions,
        (actual,),
        (("JOIN", TAG),),
        TerminalState(),
        expected_dispatch_payloads=(
            ("JOIN", TAG, payload_fingerprint("JOIN", {"left": 1, "right": 2})),
        ),
    )

    with pytest.raises(TraceViolationError, match="actual dispatch payload"):
        assert_trace_contract(trace)


def test_oracle_rejects_nonempty_raw_join_state_with_no_buckets():
    edges, resolutions, dispatches = _valid_join_case()

    with pytest.raises(TraceViolationError, match="join state nodes"):
        assert_trace_contract(
            Trace(
                edges,
                resolutions,
                dispatches,
                (("JOIN", TAG),),
                TerminalState(join_state_nodes=("JOIN",)),
            )
        )


@pytest.mark.parametrize(
    ("terminal", "message"),
    [
        (TerminalState(pending_nodes=("JOIN",)), "pending nodes"),
        (TerminalState(staged_payloads=(("JOIN", "fp"),)), "staged payloads"),
        (TerminalState(staged_tags=(("JOIN", TAG),)), "staged tags"),
        (TerminalState(join_buckets=(("JOIN", TAG),)), "join buckets"),
    ],
)
def test_oracle_rejects_nonempty_terminal_state(terminal, message):
    edges, resolutions, dispatches = _valid_join_case()

    with pytest.raises(TraceViolationError, match=message):
        assert_trace_contract(Trace(edges, resolutions, dispatches, (("JOIN", TAG),), terminal))


def test_oracle_rejects_disabled_edge_removal_mismatch():
    edges, resolutions, dispatches = _valid_join_case()
    disabled = (
        edges[0],
        EdgeSpec("right-join", "RIGHT", "JOIN", enabled=False),
    )
    disabled_trace = Trace(
        disabled,
        (resolutions[0], EdgeResolution("right-join", TAG, False, None)),
        (Dispatch("JOIN", TAG, (resolutions[0].payload_fingerprint,)),),
        (("JOIN", TAG),),
        TerminalState(),
    )
    removed_trace = Trace(
        (edges[0],),
        (resolutions[0],),
        (Dispatch("JOIN", TAG, (resolutions[0].payload_fingerprint, "ghost")),),
        (("JOIN", TAG),),
        TerminalState(),
    )

    with pytest.raises(TraceViolationError, match="disabled edge differs from removal"):
        assert_disabled_equals_removed(disabled_trace, removed_trace)


def test_oracle_rejects_replay_that_differs_from_uninterrupted_execution():
    edges, resolutions, dispatches = _valid_join_case()
    uninterrupted = Trace(edges, resolutions, dispatches, (("JOIN", TAG),), TerminalState())
    replayed = Trace(
        edges,
        resolutions,
        (Dispatch("JOIN", TAG, (resolutions[0].payload_fingerprint,)),),
        (("JOIN", TAG),),
        TerminalState(),
    )

    with pytest.raises(TraceViolationError, match="replay differs from uninterrupted"):
        assert_replay_equivalent(uninterrupted, replayed)
