"""Seeded defect registry and mutation-kill accounting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .models import Case
from .normalization import normalize_trace
from .oracle import Oracle, OracleViolation, Resolution, Trace


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    name: str
    caught: bool


Mutation = Callable[[Trace], Trace]


def _duplicate_resolution(trace: Trace) -> Trace:
    if not trace.resolutions:
        ghost = Resolution("e-ghost", "t-ghost", "n0", "n1", True, None)
        return trace.with_resolutions((ghost, ghost))
    return trace.with_resolutions((*trace.resolutions, trace.resolutions[0]))


def _drop_resolution(trace: Trace) -> Trace:
    if not trace.resolutions:
        return replace(trace, dispatches=trace.dispatches[1:])
    return trace.with_resolutions(trace.resolutions[1:])


def _duplicate_dispatch(trace: Trace) -> Trace:
    return replace(trace, dispatches=(*trace.dispatches, trace.dispatches[0]))


def _corrupt_payload(trace: Trace) -> Trace:
    if not trace.resolutions:
        return replace(trace, terminal_output={"corrupted": True})
    first = trace.resolutions[0]
    corrupted = replace(first, payload={"corrupted": True})
    return trace.with_resolutions((corrupted, *trace.resolutions[1:]))


def _retain_pending(trace: Trace) -> Trace:
    return replace(trace, pending=("ghost-token",))


MUTATIONS: tuple[tuple[str, Mutation], ...] = (
    ("duplicate_resolution", _duplicate_resolution),
    ("drop_resolution", _drop_resolution),
    ("duplicate_dispatch", _duplicate_dispatch),
    ("corrupt_payload", _corrupt_payload),
    ("retain_pending", _retain_pending),
)


def evaluate_mutations(case: Case) -> tuple[MutationOutcome, ...]:
    oracle = Oracle()
    baseline = oracle.run(case)
    normalized = normalize_trace(baseline)
    outcomes: list[MutationOutcome] = []
    for name, mutation in MUTATIONS:
        mutated = mutation(baseline)
        try:
            oracle.validate(mutated)
        except OracleViolation:
            caught = True
        else:
            caught = normalize_trace(mutated) != normalized
        outcomes.append(MutationOutcome(name, caught))
    return tuple(outcomes)
