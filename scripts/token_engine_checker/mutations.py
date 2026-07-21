"""Seeded defect registry and mutation-kill accounting."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
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


def _schedule_input_discarded(trace: Trace) -> Trace:
    first = trace.dispatches[0]
    return replace(
        trace,
        dispatches=(replace(first, token_id="schedule-was-discarded"), *trace.dispatches[1:]),
    )


def _retry_lifecycle_lost(trace: Trace) -> Trace:
    lifecycle = tuple(item for item in trace.lifecycle if item[1] != "retry")
    if lifecycle == trace.lifecycle:
        lifecycle = (*lifecycle, ("ghost-token", "retry"))
    return replace(trace, lifecycle=lifecycle)


def _terminal_output_corrupted(trace: Trace) -> Trace:
    return replace(trace, terminal_output={"corrupted_terminal": True})


def _production_state_mutation(
    trace: Trace, section: str, field: str, value: object
) -> Trace:
    persisted = deepcopy(trace.persisted_state)
    persisted["production"][section][field] = value
    return replace(trace, persisted_state=persisted)


def _join_closes_twice(trace: Trace) -> Trace:
    return _production_state_mutation(trace, "join", "state", "open")


def _loop_owner_leaks(trace: Trace) -> Trace:
    return _production_state_mutation(trace, "loop", "state", "running")


def _failure_policy_globalized(trace: Trace) -> Trace:
    current = trace.persisted_state["production"]["join"]["failure_policy"]
    replacement = "best_effort" if current == "fail_fast" else "fail_fast"
    return _production_state_mutation(trace, "join", "failure_policy", replacement)


def _cancellation_generation_lost(trace: Trace) -> Trace:
    current = trace.persisted_state["production"]["lifecycle"]["cancellation_generation"]
    return _production_state_mutation(
        trace,
        "lifecycle",
        "cancellation_generation",
        1 if current == 0 else 0,
    )


def _checkpoint_reload_skipped(trace: Trace) -> Trace:
    return _production_state_mutation(trace, "repository", "reloads", 0)


def _persisted_terminal_dropped(trace: Trace) -> Trace:
    persisted = deepcopy(trace.persisted_state)
    persisted["terminal"] = []
    if persisted == trace.persisted_state:
        persisted["terminal"] = [["ghost-token", None]]
    return replace(trace, persisted_state=persisted)


MUTATIONS: tuple[tuple[str, Mutation], ...] = (
    ("duplicate_resolution", _duplicate_resolution),
    ("drop_resolution", _drop_resolution),
    ("duplicate_dispatch", _duplicate_dispatch),
    ("corrupt_payload", _corrupt_payload),
    ("retain_pending", _retain_pending),
    ("schedule_input_discarded", _schedule_input_discarded),
    ("retry_lifecycle_lost", _retry_lifecycle_lost),
    ("terminal_output_corrupted", _terminal_output_corrupted),
    ("join_closes_twice", _join_closes_twice),
    ("loop_owner_leaks", _loop_owner_leaks),
    ("failure_policy_globalized", _failure_policy_globalized),
    ("cancellation_generation_lost", _cancellation_generation_lost),
    ("checkpoint_reload_skipped", _checkpoint_reload_skipped),
    ("persisted_terminal_dropped", _persisted_terminal_dropped),
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
