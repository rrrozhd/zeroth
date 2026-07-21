"""Seeded defect registry and mutation-kill accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Case, Edge, State, Topology
from .normalization import normalize_trace
from .oracle import Oracle


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    name: str
    caught: bool


MUTATIONS = (
    "duplicate_resolution",
    "drop_resolution",
    "duplicate_dispatch",
    "corrupt_payload",
    "retain_pending",
    "schedule_input_discarded",
    "retry_lifecycle_lost",
    "terminal_output_corrupted",
    "join_closes_twice",
    "loop_owner_leaks",
    "failure_policy_globalized",
    "cancellation_generation_lost",
    "checkpoint_reload_skipped",
    "persisted_terminal_dropped",
)


def _diamond_case(state: State) -> Case:
    return Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n0", "n2", 0),
                Edge("e2", "n1", "n3", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        ),
        (True,) * 4,
        (),
        state,
    )


def _loop_case(state: State) -> Case:
    return Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n1", "n0", 0),
                Edge("e2", "n1", "n2", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        ),
        (True,) * 4,
        (),
        state,
    )


def _seed_case(case: Case, name: str) -> tuple[Case, tuple[str, ...] | None]:
    if name == "schedule_input_discarded":
        topology = Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n0", "n2", 0),
                Edge("e2", "n1", "n3", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        )
        scheduled = Case(
            topology,
            (True, True, True, True),
            (),
            State("null", "collect", "none", "none", "none"),
        )
        return scheduled, ("t0", "t0.e1.0", "t0.e0.0")
    state = case.state
    if name in {
        "duplicate_resolution",
        "drop_resolution",
        "duplicate_dispatch",
        "corrupt_payload",
        "retain_pending",
    }:
        state = replace(state, checkpoint="none", cancellation="none")
        return _diamond_case(state), None
    if name == "retry_lifecycle_lost":
        state = replace(
            state,
            retry="fail-first",
            checkpoint="none",
            cancellation="none",
        )
        return _diamond_case(state), None
    elif name in {"join_closes_twice", "failure_policy_globalized"}:
        state = replace(state, retry="fail-first")
        return _diamond_case(state), None
    elif name == "loop_owner_leaks":
        return _loop_case(state), None
    elif name in {"terminal_output_corrupted", "persisted_terminal_dropped"}:
        state = replace(state, checkpoint="none", cancellation="none")
        return _diamond_case(state), None
    elif name == "cancellation_generation_lost":
        state = replace(state, checkpoint="after-claim", cancellation="after-cut")
    elif name == "checkpoint_reload_skipped":
        state = replace(state, checkpoint="after-claim", cancellation="none")
    return replace(case, state=state), None


def evaluate_mutations(case: Case) -> tuple[MutationOutcome, ...]:
    from .adapter import ProductionAdapter

    outcomes: list[MutationOutcome] = []
    for name in MUTATIONS:
        seeded_case, schedule = _seed_case(case, name)
        expected = normalize_trace(Oracle().run(seeded_case, schedule))
        try:
            observed = normalize_trace(
                ProductionAdapter(mutation=name).run(seeded_case, schedule)
            )
        except Exception:
            caught = True
        else:
            caught = observed != expected
        outcomes.append(MutationOutcome(name, caught))
    return tuple(outcomes)
