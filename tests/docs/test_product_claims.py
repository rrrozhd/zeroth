"""Documentation truth: product claims must match implemented behavior.

These assertions pin the specific claims the v0.9 audit found divergent —
if behavior changes again, change the docs and these tests together.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_readme_states_restart_after_deployment_creation() -> None:
    readme = _read("README.md")
    assert "service restart" in readme, (
        "README must carry the serving caveat the Studio UI shows: a newly "
        "created deployment version is not served until the service restarts"
    )


def test_readme_ties_budget_enforcement_to_regulus_extra() -> None:
    readme = _read("README.md")
    budget_line = next(line for line in readme.splitlines() if line.startswith("- **Budgets**"))
    assert "`regulus` extra" in budget_line
    assert "bare install" in budget_line, (
        "the budget bullet must say what a bare `pip install zeroth-core` does "
        "(no enforcement backend -> caps are not enforced)"
    )


def test_readme_documents_fail_open_default() -> None:
    readme = _read("README.md")
    assert "fails open by default" in readme


def test_project_md_reflects_current_architecture() -> None:
    project = _read(".planning/PROJECT.md")
    assert "Next.js" in project
    assert "0.9" in project
    assert "econ plane" in project.lower() or "econ_plane" in project
    assert "roadmap" in project.lower()
    assert "package" in project.lower()


def test_security_covers_new_hardening_surfaces() -> None:
    security = _read("SECURITY.md")
    assert "audit hash chain" in security.lower()
    assert "vault" in security.lower()
    assert "mcp" in security.lower()
