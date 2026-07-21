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


def compare_case(case: Case, *, seed: int) -> Comparison:
    ready_sets = Oracle().ready_sets(case)
    ready = max(ready_sets, key=lambda item: (len(item), item), default=("t0",))
    orders = schedule_orders(ready, seed=seed, case_digest=case.digest)
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
