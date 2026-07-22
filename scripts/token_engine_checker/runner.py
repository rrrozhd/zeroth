"""Top-level exhaustive and sampled model-checker runs."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable
from dataclasses import replace
from itertools import product
from pathlib import Path

from .explorer import (
    Comparison,
    compare_canonical_case,
    compare_case,
    compare_schedule_choices,
)
from .generator import (
    generate_topology_candidates,
    sample_cases,
)
from .models import (
    CANCELLATIONS,
    CHECKPOINTS,
    GRAMMAR_VERSION,
    PAYLOAD_JSON,
    REDUCERS,
    RETRIES,
    Case,
    State,
    Topology,
    classify_case,
    classify_topology,
)
from .mutations import evaluate_mutations
from .reporting import write_report
from .shrinker import shrink_case


def _git_sha() -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _semantic_key(case: Case) -> tuple[object, ...]:
    conditions = dict(case.conditions)
    active = tuple(
        (edge.edge_id, edge.source, edge.target, edge.parallel_ordinal)
        for edge, enabled in zip(case.topology.edges, case.enabled, strict=True)
        if enabled
        and (edge.condition is None or conditions[edge.condition] is edge.condition_value)
    )
    return (
        active,
        case.state.payload_json,
        case.state.reducer,
        case.state.retry,
        case.state.checkpoint,
        case.state.cancellation,
    )


def _exhaustive_semantic_cases(
    topologies: tuple[Topology, ...],
) -> tuple[tuple[tuple[Case, int], ...], int]:
    """Collapse only cases with identical active graphs and state atoms."""
    structural: dict[tuple[object, ...], tuple[Case, int]] = {}
    base_state = State("null", "collect", "none", "none", "none")
    for topology in topologies:
        for enabled in product((True, False), repeat=len(topology.edges)):
            for values in product((False, True), repeat=len(topology.condition_names)):
                conditions = tuple(zip(topology.condition_names, values, strict=True))
                case = Case(topology, tuple(enabled), conditions, base_state)
                if not classify_case(case).valid:
                    continue
                active_key = _semantic_key(case)[0]
                representative, count = structural.get(active_key, (case, 0))
                structural[active_key] = (representative, count + 1)

    result: list[tuple[Case, int]] = []
    logical_eligible = 0
    for representative, multiplicity in structural.values():
        for payload, reducer, retry, checkpoint, cancellation in product(
            PAYLOAD_JSON, REDUCERS, RETRIES, CHECKPOINTS, CANCELLATIONS
        ):
            if checkpoint == "none" and cancellation == "after-cut":
                continue
            case = replace(
                representative,
                state=State(payload, reducer, retry, checkpoint, cancellation),
            )
            result.append((case, multiplicity))
            logical_eligible += multiplicity
    return tuple(result), logical_eligible


def _schedule_key(case: Case) -> tuple[object, ...]:
    if case.state.cancellation == "none":
        cut = "uncancelled"
    elif case.state.checkpoint == "before-claim":
        cut = "cancel-before-claim"
    elif case.state.checkpoint in {"after-claim", "before-dispatch"}:
        cut = "cancel-after-claim"
    else:
        cut = "cancel-after-resolve"
    return (_semantic_key(case)[0], cut)


def _failure(case: Case, comparison: Comparison, seed: int) -> dict[str, object]:
    def still_fails(candidate: Case) -> bool:
        return not compare_case(candidate, seed=seed).passed

    minimized = shrink_case(case, still_fails)
    return {
        "kind": comparison.failure_kind or "unknown",
        "case": case.to_json(),
        "trace": {"detail": comparison.detail},
        "minimized": minimized.to_json(),
    }


def _case_candidate_count(topology: Topology) -> int:
    state_atoms = (
        len(PAYLOAD_JSON)
        * len(REDUCERS)
        * len(RETRIES)
        * len(CHECKPOINTS)
        * len(CANCELLATIONS)
    )
    return 2 ** (len(topology.edges) + len(topology.condition_names)) * state_atoms


def run_check(
    *,
    nodes: int,
    exhaustive: bool = False,
    cases: int | None = None,
    seed: int | None = None,
    report_path: Path | str | None = None,
) -> dict[str, object]:
    if exhaustive:
        if nodes != 4 or cases is not None:
            raise ValueError("exhaustive mode is defined only for N=4 without --cases")
    elif nodes not in {5, 6} or cases is None or seed is None:
        raise ValueError("sampled mode requires N=5 or N=6, --cases, and --seed")

    started = time.perf_counter()
    topology_candidates = tuple(generate_topology_candidates(nodes))
    valid_topologies = tuple(
        topology for topology in topology_candidates if classify_topology(topology).valid
    )
    if exhaustive:
        semantic_cases, logical_eligible = _exhaustive_semantic_cases(valid_topologies)
        stream: Iterable[tuple[Case, int]] = semantic_cases
        mode = "exhaustive"
    else:
        stream = tuple(
            (case, 1)
            for case in sample_cases(nodes, count=cases or 0, seed=seed or 0)
        )
        logical_eligible = 0
        mode = "sampled"

    count_names = ("candidate", "invalid", "eligible", "executed", "passed", "failed")
    counts = {key: 0 for key in count_names}
    total_exhaustive_candidates = (
        sum(
            _case_candidate_count(topology)
            for topology in topology_candidates
        )
        if exhaustive
        else 0
    )
    counts["candidate"] = total_exhaustive_candidates
    counts["invalid"] = total_exhaustive_candidates - logical_eligible
    schedule_eligible = 0
    schedule_executed = 0
    eligible_topologies: set[str] = set()
    executed_topologies: set[str] = set()
    failures: list[dict[str, object]] = []
    cache: dict[tuple[object, ...], Comparison] = {}
    schedule_cache: dict[tuple[object, ...], Comparison] = {}
    cache_hits = 0
    schedule_cache_hits = 0
    transition_invocations = 0
    state_transition_invocations = 0
    schedule_transition_invocations = 0
    mutation_case: Case | None = None

    for case, multiplicity in stream:
        if not exhaustive:
            counts["candidate"] += 1
            classification = classify_case(case)
            if not classification.valid:
                counts["invalid"] += 1
                continue
        counts["eligible"] += multiplicity
        eligible_topologies.add(case.topology.digest)
        mutation_case = mutation_case or case
        key = _semantic_key(case)
        state_comparison = cache.get(key)
        if state_comparison is None:
            state_comparison = compare_canonical_case(case)
            cache[key] = state_comparison
            state_transition_invocations += state_comparison.schedules_executed
            cache_hits += multiplicity - 1
        else:
            cache_hits += multiplicity
        schedule_key = _schedule_key(case)
        schedule_comparison = schedule_cache.get(schedule_key)
        if schedule_comparison is None:
            schedule_comparison = compare_schedule_choices(case, seed=seed or 0)
            schedule_cache[schedule_key] = schedule_comparison
            schedule_transition_invocations += (
                schedule_comparison.transition_invocations
                if schedule_comparison.transition_invocations is not None
                else schedule_comparison.schedules_executed
            )
        else:
            schedule_cache_hits += multiplicity
        schedule_eligible += schedule_comparison.schedules_eligible * multiplicity
        schedule_executed += schedule_comparison.schedules_executed * multiplicity
        transition_invocations = (
            state_transition_invocations + schedule_transition_invocations
        )
        comparison = (
            state_comparison if not state_comparison.passed else schedule_comparison
        )
        counts["executed"] += multiplicity
        executed_topologies.add(case.topology.digest)
        if comparison.passed:
            counts["passed"] += multiplicity
        else:
            counts["failed"] += multiplicity
            failures.append(_failure(case, comparison, seed or 0))

    mutations = (
        []
        if mutation_case is None
        else [
            {"name": outcome.name, "caught": outcome.caught}
            for outcome in evaluate_mutations(mutation_case)
        ]
    )
    complete = counts["eligible"] == counts["executed"]
    requested = exhaustive or counts["executed"] == cases
    status = (
        "passed"
        if complete
        and requested
        and counts["failed"] == 0
        and mutations
        and all(item["caught"] for item in mutations)
        else "failed"
    )
    exhaustive_schedules = mode == "exhaustive"
    executed_topology_count = (
        len(valid_topologies) if exhaustive and complete else len(executed_topologies)
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "grammar_version": GRAMMAR_VERSION,
        "git_sha": _git_sha(),
        "command": {
            "nodes": nodes,
            "mode": mode,
            "cases": cases,
            "seed": seed,
        },
        "status": status,
        "counts": counts,
        "coverage": {
            "topology": {
                "candidate": len(topology_candidates),
                "invalid": len(topology_candidates) - len(valid_topologies),
                "eligible": len(valid_topologies),
                "executed": executed_topology_count,
            },
            "state": {"eligible": counts["eligible"], "executed": counts["executed"]},
            "exhaustive_schedule": {
                "eligible": schedule_eligible if exhaustive_schedules else 0,
                "executed": schedule_executed if exhaustive_schedules else 0,
            },
            "sampled_schedule": {
                "eligible": 0 if exhaustive_schedules else schedule_eligible,
                "executed": 0 if exhaustive_schedules else schedule_executed,
            },
        },
        "seeds": [] if seed is None else [seed],
        "mutations": mutations,
        "failures": failures,
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "transition_invocations": transition_invocations,
        "cached_transition_invocations": {
            "state": state_transition_invocations,
            "schedule": schedule_transition_invocations,
            "total": transition_invocations,
        },
        "cache": {
            "semantic_cases": len(cache),
            "hits": cache_hits,
            "schedule_classes": len(schedule_cache),
            "schedule_hits": schedule_cache_hits,
        },
    }
    if report_path is not None:
        write_report(report_path, report)
    return report
