"""Canonical finite grammar enumeration and deterministic sampling."""

from __future__ import annotations

import random
from collections.abc import Iterator
from itertools import combinations_with_replacement, permutations, product

from .models import (
    CANCELLATIONS,
    CHECKPOINTS,
    PAYLOAD_JSON,
    REDUCERS,
    RETRIES,
    Case,
    State,
    Topology,
    canonicalize_edges,
    classify_case,
    classify_topology,
    condition_edges,
)


def generate_topologies(nodes: int) -> Iterator[Topology]:
    if nodes < 2:
        raise ValueError("nodes must be at least two")
    labels = tuple(f"n{index}" for index in range(nodes))
    allowed = tuple(
        (source, target) for source in labels[:-1] for target in labels if source != target
    )
    seen: set[str] = set()
    if nodes == 4:
        accepted: list[Topology] = []
        for edge_count in range(1, nodes + 2):
            for pairs in combinations_with_replacement(allowed, edge_count):
                topology = Topology(
                    labels,
                    condition_edges(canonicalize_edges(pairs)),
                )
                if topology.digest in seen or not classify_topology(topology).valid:
                    continue
                seen.add(topology.digest)
                accepted.append(topology)
        yield from sorted(
            accepted,
            key=lambda topology: (
                len(topology.condition_names),
                len(topology.edges),
                topology.digest,
            ),
        )
        return
    for interior in permutations(labels[1:-1]):
        path = (labels[0], *interior, labels[-1])
        spine = tuple(zip(path[:-1], path[1:], strict=True))
        extras = ((), *((pair,) for pair in allowed), *combinations_with_replacement(allowed, 2))
        for extra in extras:
            edges = condition_edges(canonicalize_edges((*spine, *extra)))
            topology = Topology(labels, edges)
            if topology.digest in seen or not classify_topology(topology).valid:
                continue
            seen.add(topology.digest)
            yield topology


def enumerate_cases(topology: Topology) -> Iterator[Case]:
    condition_names = topology.condition_names
    for enabled in product((True, False), repeat=len(topology.edges)):
        for values in product((False, True), repeat=len(condition_names)):
            conditions = tuple(zip(condition_names, values, strict=True))
            for payload, reducer, retry, checkpoint, cancellation in product(
                PAYLOAD_JSON, REDUCERS, RETRIES, CHECKPOINTS, CANCELLATIONS
            ):
                yield Case(
                    topology,
                    tuple(enabled),
                    conditions,
                    State(payload, reducer, retry, checkpoint, cancellation),
                )


def eligible_cases(nodes: int) -> Iterator[Case]:
    for topology in generate_topologies(nodes):
        for case in enumerate_cases(topology):
            if classify_case(case).valid:
                yield case


def sample_cases(nodes: int, *, count: int, seed: int) -> tuple[Case, ...]:
    if count < 0:
        raise ValueError("count must be non-negative")
    rng = random.Random(seed)
    topologies = tuple(generate_topologies(nodes))
    if not topologies and count:
        raise ValueError("grammar produced no eligible topologies")
    selected: dict[str, Case] = {}
    attempts = 0
    maximum_attempts = max(1000, count * 100)
    while len(selected) < count and attempts < maximum_attempts:
        topology = topologies[rng.randrange(len(topologies))]
        enabled = tuple(bool(rng.getrandbits(1)) for _ in topology.edges)
        conditions = tuple((name, bool(rng.getrandbits(1))) for name in topology.condition_names)
        state = State(
            rng.choice(PAYLOAD_JSON),
            rng.choice(REDUCERS),
            rng.choice(RETRIES),
            rng.choice(CHECKPOINTS),
            rng.choice(CANCELLATIONS),
        )
        case = Case(topology, enabled, conditions, state)
        if classify_case(case).valid:
            selected.setdefault(case.digest, case)
        attempts += 1
    if len(selected) != count:
        raise ValueError(f"grammar produced only {len(selected)} sampled cases; need {count}")
    return tuple(selected[digest] for digest in sorted(selected))
