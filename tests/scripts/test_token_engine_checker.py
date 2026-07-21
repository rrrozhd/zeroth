from __future__ import annotations

import json
from pathlib import Path

import scripts.token_engine_checker.runner as checker_runner

from scripts.token_engine_checker.adapter import ProductionAdapter
from scripts.token_engine_checker.explorer import Comparison
from scripts.token_engine_checker.generator import enumerate_cases, generate_topologies
from scripts.token_engine_checker.models import (
    Case,
)
from scripts.token_engine_checker.mutations import _seed_case, evaluate_mutations
from scripts.token_engine_checker.reporting import write_report
from scripts.token_engine_checker.runner import run_check
from scripts.token_engine_checker.shrinker import shrink_case


def _case_with_extra_edges() -> Case:
    topology = next(topology for topology in generate_topologies(4) if len(topology.edges) == 5)
    return next(case for case in enumerate_cases(topology) if all(case.enabled))


def test_production_adapter_honors_schedule_of_actual_ready_token_ids() -> None:
    topology = next(
        topology
        for topology in generate_topologies(4)
        if {(edge.source, edge.target) for edge in topology.edges}
        == {("n0", "n1"), ("n0", "n2"), ("n1", "n3"), ("n2", "n3")}
    )
    case = next(case for case in enumerate_cases(topology) if all(case.enabled))
    root_edges = [edge for edge in topology.edges if edge.source == "n0"]
    first, second = root_edges
    schedule = ("t0", f"t0.{second.edge_id}.0", f"t0.{first.edge_id}.0")

    trace = ProductionAdapter().run(case, schedule)

    assert trace.dispatches[1].token_id == schedule[1]


def test_every_registered_mutation_is_caught() -> None:
    outcomes = evaluate_mutations(_case_with_extra_edges())

    assert {
        "schedule_input_discarded",
        "retry_lifecycle_lost",
        "join_closes_twice",
        "loop_owner_leaks",
        "failure_policy_globalized",
        "cancellation_generation_lost",
        "checkpoint_reload_skipped",
        "persisted_terminal_dropped",
    } <= {outcome.name for outcome in outcomes}
    assert all(outcome.caught for outcome in outcomes)
    assert len({outcome.name for outcome in outcomes}) == len(outcomes)


def test_structured_mutation_seeds_exercise_the_defect_precondition() -> None:
    base = _case_with_extra_edges()

    join_case, _ = _seed_case(base, "join_closes_twice")
    policy_case, _ = _seed_case(base, "failure_policy_globalized")
    loop_case, _ = _seed_case(base, "loop_owner_leaks")
    checkpoint_case, _ = _seed_case(base, "checkpoint_reload_skipped")

    join = ProductionAdapter().run(join_case).persisted_state["production"]["join"]
    policy = ProductionAdapter().run(policy_case).persisted_state["production"]["join"]
    loop = ProductionAdapter().run(loop_case).persisted_state["production"]["loop"]

    assert join["state"] == "closed"
    assert len(join["edge_ids"]) >= 2
    assert policy["state"] == "closed"
    assert policy["failure_policy"] == "best_effort"
    assert loop["state"] == "completed"
    assert checkpoint_case.state.checkpoint != "none"


def test_mutations_change_production_execution_before_trace_construction() -> None:
    base = _case_with_extra_edges()
    seeded, schedule = _seed_case(base, "checkpoint_reload_skipped")

    normal = ProductionAdapter().run(seeded, schedule)
    mutated = ProductionAdapter(mutation="checkpoint_reload_skipped").run(
        seeded, schedule
    )

    normal_graph = normal.persisted_state["production"]["graph_execution"]
    mutated_graph = mutated.persisted_state["production"]["graph_execution"]
    assert normal_graph["checkpoint_reloads"] == 1
    assert mutated_graph["checkpoint_reloads"] == 0


def test_shrinker_preserves_failure_while_reducing_case() -> None:
    case = _case_with_extra_edges()

    shrunk = shrink_case(case, lambda candidate: len(candidate.topology.edges) >= 4)

    assert len(shrunk.topology.edges) == 4
    assert len(shrunk.topology.edges) < len(case.topology.edges)


def test_json_report_write_is_atomic_and_canonical(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("old", encoding="utf-8")

    write_report(report_path, {"z": 1, "a": 2})

    assert json.loads(report_path.read_text(encoding="utf-8")) == {"a": 2, "z": 1}
    assert not tuple(tmp_path.glob(".report.json.*.tmp"))


def test_sampled_run_reports_requested_eligible_and_executed_counts(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"

    report = run_check(nodes=5, cases=12, seed=120500, report_path=path)

    assert report["status"] == "passed"
    assert report["grammar_version"] == "grammar-v1"
    assert report["counts"]["eligible"] == 12
    assert report["counts"]["executed"] == 12
    assert report["coverage"]["sampled_schedule"]["executed"] > 0
    assert report["seeds"] == [120500]
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_exhaustive_report_counts_invalid_topology_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        checker_runner,
        "_exhaustive_semantic_cases",
        lambda _topologies: ((), 0),
    )
    monkeypatch.setattr(
        checker_runner,
        "compare_case",
        lambda *_args, **_kwargs: Comparison(True, 1, 1),
    )

    report = run_check(nodes=4, exhaustive=True)

    topology = report["coverage"]["topology"]
    assert topology["candidate"] == 2002
    assert topology["invalid"] == 1714
    assert topology["eligible"] == 288


def test_exhaustive_semantic_classes_preserve_exact_logical_multiplicity() -> None:
    topologies = tuple(generate_topologies(4))

    representatives, logical_eligible = checker_runner._exhaustive_semantic_cases(
        topologies
    )

    assert logical_eligible == 2_353_104
    assert len(representatives) == 152_064
    assert sum(multiplicity for _case, multiplicity in representatives) == logical_eligible


def test_cached_cases_separate_logical_coverage_from_transition_invocations(
    tmp_path: Path, monkeypatch
) -> None:
    cases_by_key: dict[tuple[object, ...], Case] = {}
    duplicate: tuple[Case, Case] | None = None
    for topology in generate_topologies(5):
        for case in enumerate_cases(topology):
            if not checker_runner.classify_case(case).valid:
                continue
            key = checker_runner._semantic_key(case)
            if key in cases_by_key and cases_by_key[key].digest != case.digest:
                duplicate = (cases_by_key[key], case)
                break
            cases_by_key[key] = case
        if duplicate is not None:
            break
    assert duplicate is not None

    monkeypatch.setattr(checker_runner, "sample_cases", lambda *_args, **_kwargs: duplicate)
    monkeypatch.setattr(
        checker_runner,
        "compare_schedule_choices",
        lambda *_args, **_kwargs: Comparison(
            True, 3, 3, transition_invocations=3
        ),
    )
    monkeypatch.setattr(
        checker_runner,
        "compare_canonical_case",
        lambda *_args, **_kwargs: Comparison(True, 1, 1),
    )

    report = run_check(
        nodes=5,
        cases=2,
        seed=120500,
        report_path=tmp_path / "cached.json",
    )

    assert report["counts"]["eligible"] == 2
    assert report["counts"]["executed"] == 2
    assert report["coverage"]["sampled_schedule"] == {"eligible": 6, "executed": 6}
    assert report["transition_invocations"] == 4
    assert report["cached_transition_invocations"] == {
        "state": 1,
        "schedule": 3,
        "total": 4,
    }
    assert report["cache"] == {
        "semantic_cases": 1,
        "hits": 1,
        "schedule_classes": 1,
        "schedule_hits": 1,
    }


def test_sampled_run_is_deterministic_except_measured_runtime(tmp_path: Path) -> None:
    first = run_check(nodes=5, cases=8, seed=120500, report_path=tmp_path / "first.json")
    second = run_check(nodes=5, cases=8, seed=120500, report_path=tmp_path / "second.json")

    first.pop("runtime_seconds")
    second.pop("runtime_seconds")
    assert first == second
