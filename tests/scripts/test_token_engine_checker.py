from __future__ import annotations

import json
from pathlib import Path

from scripts.token_engine_checker.generator import enumerate_cases, generate_topologies
from scripts.token_engine_checker.models import Case
from scripts.token_engine_checker.mutations import evaluate_mutations
from scripts.token_engine_checker.reporting import write_report
from scripts.token_engine_checker.runner import run_check
from scripts.token_engine_checker.shrinker import shrink_case


def _case_with_extra_edges() -> Case:
    topology = next(topology for topology in generate_topologies(4) if len(topology.edges) == 5)
    return next(case for case in enumerate_cases(topology) if all(case.enabled))


def test_every_registered_mutation_is_caught() -> None:
    outcomes = evaluate_mutations(_case_with_extra_edges())

    assert len(outcomes) >= 5
    assert all(outcome.caught for outcome in outcomes)
    assert len({outcome.name for outcome in outcomes}) == len(outcomes)


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


def test_sampled_run_is_deterministic_except_measured_runtime(tmp_path: Path) -> None:
    first = run_check(nodes=5, cases=8, seed=120500, report_path=tmp_path / "first.json")
    second = run_check(nodes=5, cases=8, seed=120500, report_path=tmp_path / "second.json")

    first.pop("runtime_seconds")
    second.pop("runtime_seconds")
    assert first == second
