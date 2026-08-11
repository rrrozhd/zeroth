"""Release metadata coherence."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def test_uv_lock_tracks_project_version() -> None:
    expected_version = project_version()
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    local_versions = {
        package["name"]: package["version"]
        for package in lock["package"]
        if "source" in package
        and (
            package["source"].get("editable") == "."
            or package["source"].get("directory") == "packaging/console"
        )
    }
    assert local_versions == {
        "zeroth-console": expected_version,
        "zeroth-core": expected_version,
    }


def test_console_package_version_tracks_project_version() -> None:
    expected_version = project_version()
    console = tomllib.loads(
        (REPO_ROOT / "packaging/console/pyproject.toml").read_text(encoding="utf-8")
    )
    assert console["project"]["version"] == expected_version


def test_frontend_version_tracks_project_version() -> None:
    expected_version = project_version()
    frontend = (REPO_ROOT / "frontend/app/lib/version.ts").read_text(encoding="utf-8")
    frontend_test = (REPO_ROOT / "frontend/app/lib/version.test.ts").read_text(encoding="utf-8")
    assert f'export const VERSION = "{expected_version}";' in frontend
    assert f'toBe("{expected_version}")' in frontend_test


def test_langgraph_release_version_tracks_project_version() -> None:
    expected_version = project_version()
    guide = (REPO_ROOT / "docs/how-to/deployment/langgraph-release.md").read_text(encoding="utf-8")
    assert (
        f"This is the canonical clean install and operations path for Zeroth `{expected_version}`."
        in guide
    )
    assert f"zeroth-core[langgraph,langgraph-gateway]=={expected_version}" in guide


def test_changelog_documents_the_hardening_release() -> None:
    expected_version = project_version()
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{expected_version}]" in changelog
    # The six hardening workstreams live in the 0.9.1 entry regardless of
    # later hotfix versions.
    _, _, release_notes = changelog.partition("## [0.9.1]")
    release_notes = release_notes.split("\n## [", 1)[0]
    # The six hardening workstreams must each be represented.
    for marker in (
        "isolat",  # runtime isolation
        "tenant",  # tenant-safe deployments
        "coordinat",  # database coordination
        "retention",  # retention correctness
        "MCP",
        "Vault",
    ):
        assert marker in release_notes, f"missing workstream marker: {marker}"


def test_changelog_accounts_for_versions_since_0_2() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for version in ("0.9", "0.8", "0.7", "0.6", "0.5", "0.4", "0.3"):
        assert f"## [{version}]" in changelog, f"missing entry for {version}"
