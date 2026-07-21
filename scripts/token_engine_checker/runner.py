"""Top-level exhaustive and sampled model-checker runs."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from .explorer import Comparison, compare_case
from .generator import enumerate_cases, generate_topologies, sample_cases
from .models import GRAMMAR_VERSION, Case, classify_case
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
        edge.edge_id
        for edge, enabled in zip(case.topology.edges, case.enabled, strict=True)
        if enabled
        and (edge.condition is None or conditions[edge.condition] is edge.condition_value)
    )
    return (
        case.topology.digest,
        active,
        case.state.payload_json,
        case.state.reducer,
        case.state.retry,
        case.state.checkpoint,
        case.state.cancellation,
    )


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
    topology_candidates = tuple(generate_topologies(nodes))
    if exhaustive:
        stream: Iterable[Case] = (
            case for topology in topology_candidates for case in enumerate_cases(topology)
        )
        mode = "exhaustive"
    else:
        stream = sample_cases(nodes, count=cases or 0, seed=seed or 0)
        mode = "sampled"

    count_names = ("candidate", "invalid", "eligible", "executed", "passed", "failed")
    counts = {key: 0 for key in count_names}
    schedule_eligible = 0
    schedule_executed = 0
    eligible_topologies: set[str] = set()
    executed_topologies: set[str] = set()
    failures: list[dict[str, object]] = []
    cache: dict[tuple[object, ...], Comparison] = {}
    cache_hits = 0
    transition_invocations = 0
    mutation_case: Case | None = None

    for case in stream:
        counts["candidate"] += 1
        classification = classify_case(case)
        if not classification.valid:
            counts["invalid"] += 1
            continue
        counts["eligible"] += 1
        eligible_topologies.add(case.topology.digest)
        mutation_case = mutation_case or case
        key = _semantic_key(case)
        comparison = cache.get(key)
        if comparison is None:
            comparison = compare_case(case, seed=seed or 0)
            cache[key] = comparison
            schedule_eligible += comparison.schedules_eligible
            schedule_executed += comparison.schedules_executed
            transition_invocations += comparison.schedules_executed
        else:
            cache_hits += 1
        counts["executed"] += 1
        executed_topologies.add(case.topology.digest)
        if comparison.passed:
            counts["passed"] += 1
        else:
            counts["failed"] += 1
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
                "eligible": len(eligible_topologies),
                "executed": len(executed_topologies),
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
        "cache": {"semantic_cases": len(cache), "hits": cache_hits},
    }
    if report_path is not None:
        write_report(report_path, report)
    return report
