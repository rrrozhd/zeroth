"""Regression coverage for the bounded, four-runner pull-request test gate."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
DURATIONS_PATH = ROOT / ".test_durations"


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_ci_partitions_the_full_suite_across_four_bounded_runners() -> None:
    workflow = _workflow()
    test_job = workflow["jobs"]["test"]

    assert test_job["strategy"] == {
        "fail-fast": False,
        "matrix": {"group": [1, 2, 3, 4]},
    }
    assert test_job["timeout-minutes"] == 30

    scripts = "\n".join(step.get("run", "") for step in test_job["steps"])
    assert "uv run pytest -v --no-header -ra" in scripts
    assert "--splits 4 --group ${{ matrix.group }}" in scripts
    assert "--durations-path .test_durations" in scripts
    assert "--splitting-algorithm duration_based_chunks" in scripts
    assert "--timeout=300 --timeout-method=thread" in scripts


def test_ci_keeps_lint_separate_and_cancels_superseded_runs() -> None:
    workflow = _workflow()

    assert workflow["on"]["push"] == {"branches": ["main"]}
    assert workflow["on"]["pull_request"] is None
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }

    lint_job = workflow["jobs"]["lint"]
    scripts = "\n".join(step.get("run", "") for step in lint_job["steps"])
    assert lint_job["timeout-minutes"] == 20
    assert "uv run ruff check src tests" in scripts
    assert "uv run interrogate src/zeroth" in scripts
    assert "uv run pytest" not in scripts


def test_ci_sharding_plugins_are_declared_as_dev_dependencies() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert '"pytest-split>=0.9"' in pyproject
    assert '"pytest-timeout>=2.3"' in pyproject


def test_ci_duration_baseline_is_substantial_and_well_formed() -> None:
    durations = json.loads(DURATIONS_PATH.read_text(encoding="utf-8"))

    assert len(durations) >= 10_000
    assert all(nodeid.startswith("tests/") for nodeid in durations)
    assert all(
        isinstance(duration, (int, float)) and duration >= 0
        for duration in durations.values()
    )
    assert max(durations.values()) > 1
