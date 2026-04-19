from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_docstring_tooling() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "interrogate" in pyproject
    assert 'fail-under = 90' in pyproject
    assert 'convention = "google"' in pyproject


def test_ci_workflow_runs_docstring_gate() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.exists(), f"expected workflow at {workflow_path}"

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "uv sync --all-groups" in workflow
    assert "uv run ruff check src tests" in workflow
    assert "uv run pytest -v --no-header -ra" in workflow
    assert "uv run interrogate src/zeroth/core" in workflow
