"""Release metadata coherence for v0.9.1."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_VERSION = "0.10.0.0.4"


def test_project_version_matches_release() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == EXPECTED_VERSION


def test_uv_lock_tracks_project_version() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'version = "{EXPECTED_VERSION}"' in lock


def test_changelog_documents_the_hardening_release() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{EXPECTED_VERSION}]" in changelog
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
