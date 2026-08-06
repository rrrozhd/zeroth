from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_docstring_tooling() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "interrogate" in pyproject
    # ZER-25 moved this from 90 to 84 because the measured tree changed, not the
    # bar: CI used to run interrogate over ``src/zeroth/core``, a shim-dominated
    # package that scored artificially high. The gate is asserted to exist and to
    # be a real threshold, not pinned to a number this test would have to chase.
    threshold = int(re.search(r"fail-under = (\d+)", pyproject).group(1))
    assert threshold >= 80
    assert 'convention = "google"' in pyproject


def test_ci_workflow_runs_docstring_gate() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.exists(), f"expected workflow at {workflow_path}"

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "uv sync --all-groups" in workflow
    assert "uv run ruff check src tests" in workflow
    assert "uv run pytest -v --no-header -ra" in workflow
    assert "uv run interrogate src/zeroth" in workflow
