"""Deterministic scheduling and oracle/SUT comparison."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from itertools import permutations

from .adapter import ProductionAdapter, UnsupportedValidCaseError
from .models import Case
from .normalization import normalize_trace
from .oracle import Oracle, OracleViolation


@dataclass(frozen=True, slots=True)
class Comparison:
    passed: bool
    schedules_eligible: int
    schedules_executed: int
    failure_kind: str | None = None
    detail: str | None = None
    transition_invocations: int | None = None


def schedule_orders(
    ready: tuple[str, ...], *, seed: int, case_digest: str
) -> tuple[tuple[str, ...], ...]:
    canonical = tuple(sorted(ready))
    if len(canonical) <= 6:
        return tuple(permutations(canonical))
    reverse = tuple(reversed(canonical))
    material = f"{seed}:{case_digest}".encode()
    derived = seed ^ int(hashlib.sha256(material).hexdigest()[:16], 16)
    shuffled = list(canonical)
    random.Random(derived).shuffle(shuffled)
    orders = (canonical, reverse, tuple(shuffled))
    if len(set(orders)) != 3:
        shuffled = [*canonical[1:], canonical[0]]
        orders = (canonical, reverse, tuple(shuffled))
    return orders


def schedule_plans(
    ready_sets: tuple[tuple[str, ...], ...], *, seed: int, case_digest: str
) -> tuple[tuple[str, ...], ...]:
    """Enumerate the declared choices for every observed durable ready state."""
    if not ready_sets:
        return (("t0",),)
    plans: dict[tuple[str, ...], None] = {}
    for ready in ready_sets:
        for order in schedule_orders(ready, seed=seed, case_digest=case_digest):
            plans.setdefault(order, None)
    return tuple(plans)


def discover_schedule_plans(
    case: Case, *, seed: int
) -> tuple[tuple[str, ...], ...]:
    """Reach a fixed point over ready states exposed by alternate choices."""
    plans, _ready_sets = _discover_schedule_space(case, seed=seed)
    return plans


def _discover_schedule_space(
    case: Case, *, seed: int
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[tuple[tuple[str, ...], tuple[str, ...]], ...],
]:
    pending = [("t0",)]
    discovered: dict[tuple[str, ...], None] = {}
    ready_states: dict[tuple[str, ...], tuple[str, ...]] = {}
    while pending:
        schedule = pending.pop()
        if schedule in discovered:
            continue
        discovered[schedule] = None
        states: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        Oracle().run(case, schedule, _ready_states=states)
        for prefix, ready in states:
            if ready in ready_states:
                continue
            ready_states[ready] = prefix
            for order in schedule_orders(ready, seed=seed, case_digest=case.digest):
                plan = (*prefix, *order)
                if plan not in discovered:
                    pending.append(plan)
    return tuple(discovered), tuple(
        (prefix, ready) for ready, prefix in ready_states.items()
    )


def compare_schedule_choices(case: Case, *, seed: int) -> Comparison:
    """Prove every logical plan by composing actual ready-token choices."""
    plans, ready_states = _discover_schedule_space(case, seed=seed)
    invocations = 0
    adapter = ProductionAdapter()
    for prefix, ready in ready_states:
        for order in schedule_orders(ready, seed=seed, case_digest=case.digest):
            invocations += 1
            try:
                observed = adapter.verify_case_schedule_prefix(case, prefix, order)
            except Exception as error:
                return Comparison(
                    False,
                    len(plans),
                    0,
                    "schedule_transition_error",
                    str(error),
                    invocations,
                )
            expected = (*prefix, *order)
            if observed != expected:
                return Comparison(
                    False,
                    len(plans),
                    0,
                    "schedule_choice_mismatch",
                    f"requested {expected!r}, observed {observed!r}",
                    invocations,
                )
    return Comparison(True, len(plans), len(plans), transition_invocations=invocations)


def compare_case(case: Case, *, seed: int) -> Comparison:
    orders = discover_schedule_plans(case, seed=seed)
    return _compare_orders(case, orders)


def compare_canonical_case(case: Case) -> Comparison:
    """Compare one canonical execution for a state-equivalence class."""
    return _compare_orders(case, (("t0",),))


def _compare_orders(
    case: Case, orders: tuple[tuple[str, ...], ...]
) -> Comparison:
    executed = 0
    for order in orders:
        try:
            expected = normalize_trace(Oracle().run(case, order))
            observed = normalize_trace(ProductionAdapter().run(case, order))
        except UnsupportedValidCaseError as error:
            return Comparison(False, len(orders), executed, "unsupported_valid_case", str(error))
        except OracleViolation as error:
            return Comparison(False, len(orders), executed, "oracle_violation", str(error))
        executed += 1
        if observed != expected:
            return Comparison(
                False,
                len(orders),
                executed,
                "trace_mismatch",
                "normalized traces differ",
            )
    return Comparison(True, len(orders), executed)
