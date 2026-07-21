"""Deterministic validity-preserving counterexample shrinking."""

from __future__ import annotations

from collections.abc import Callable

from .models import (
    Case,
    Topology,
    canonicalize_edges,
    classify_case,
    classify_topology,
    condition_edges,
)


def _without_edge(case: Case, removed: int) -> Case | None:
    pairs = [
        (edge.source, edge.target)
        for index, edge in enumerate(case.topology.edges)
        if index != removed
    ]
    topology = Topology(case.topology.nodes, condition_edges(canonicalize_edges(pairs)))
    if not classify_topology(topology).valid:
        return None
    enabled = tuple(value for index, value in enumerate(case.enabled) if index != removed)
    old_conditions = dict(case.conditions)
    conditions = tuple((name, old_conditions.get(name, False)) for name in topology.condition_names)
    candidate = Case(topology, enabled, conditions, case.state)
    return candidate if classify_case(candidate).valid else None


def shrink_case(case: Case, fails: Callable[[Case], bool]) -> Case:
    """Greedily remove edges while validity and the supplied failure remain."""
    if not fails(case):
        raise ValueError("initial case does not reproduce the failure")
    current = case
    changed = True
    while changed:
        changed = False
        for index in range(len(current.topology.edges) - 1, -1, -1):
            candidate = _without_edge(current, index)
            if candidate is not None and fails(candidate):
                current = candidate
                changed = True
                break
    return current
