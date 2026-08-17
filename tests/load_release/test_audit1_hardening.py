"""Regressions for ZER-33 AUDIT-1 evidence boundaries."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from tests.load_release.test_report import _identity, _rows


ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "release/load/profiles-v1.json"
BASELINE = ROOT / "release/load/baseline-v1.json"
WORKFLOW = ROOT / ".github/workflows/release-gates.yml"


@pytest.mark.parametrize("mutation", ["swapped", "duplicate", "terminal-first"])
def test_accepted_run_has_one_matching_later_terminal(mutation: str) -> None:
    from release.load.report import evidence_errors, load_profiles

    rows = _rows()
    first, second = rows[:2]
    first_terminal = first["lifecycle"][-1]
    if mutation == "swapped":
        first_terminal["run_id"] = second["lifecycle"][1]["run_id"]
    elif mutation == "duplicate":
        first["lifecycle"].append(copy.deepcopy(first_terminal))
    else:
        first["lifecycle"] = [first_terminal, *first["lifecycle"][:-1]]

    errors = evidence_errors(rows, load_profiles(PROFILES))

    assert any("terminal" in error.lower() for error in errors)


def test_report_rejects_self_overlap_and_environment_mismatch() -> None:
    from release.load.report import build_report, load_baseline, load_profiles

    baseline = load_baseline(BASELINE)
    identity = _identity()
    identity["package"]["artifacts"] = {"wheel": "sha256:" + "a" * 64}
    candidate_environment = dict(baseline["environment"])
    candidate_environment["cpu_limit"] += 1

    report = build_report(
        load_profiles(PROFILES),
        baseline,
        identity,
        _rows(),
        environment=candidate_environment,
        observation_digest=baseline["source"]["run_digests"][0],
    )

    assert report["passed"] is False
    assert any("environment" in error.lower() for error in report["errors"])
    assert any(
        "baseline" in error.lower() and "overlap" in error.lower() for error in report["errors"]
    )


def test_load_gate_runs_in_the_pinned_capacity_environment() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["load-recovery"]
    container = job["container"]

    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert container["image"].startswith("python:3.12.13-slim-bookworm@sha256:")
    assert "--cpus 2" in container["options"]
    assert "--memory 8g" in container["options"]
    assert job["env"]["ZEROTH_LOAD_RUNTIME_IMAGE"] == container["image"]
    assert job["env"]["ZEROTH_LOAD_POSTGRES_VERSION"] == job["services"]["postgres"]["image"]
    assert job["env"]["ZEROTH_LOAD_REDIS_VERSION"] == job["services"]["redis"]["image"]


def test_baseline_sources_are_three_distinct_base_runs() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    source = baseline["source"]

    assert len(source["run_digests"]) == source["sample_run_count"] == 3
    assert len(set(source["run_digests"])) == 3
    assert all(value.startswith("sha256:") and len(value) == 71 for value in source["run_digests"])
    assert baseline["environment"]["runtime_image"].startswith(
        "python:3.12.13-slim-bookworm@sha256:"
    )
