"""Release workflow invariants for the standalone Zeroth SDK."""

from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).parents[2]
ROOT_README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "release-zeroth-sdk.yml"
SDK_PYPROJECT = ROOT / "packaging" / "sdk" / "pyproject.toml"
SDK_README = ROOT / "packaging" / "sdk" / "README.md"


def test_sdk_release_workflow_uses_registry_scoped_oidc_publishers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "environment: testpypi" in workflow
    assert "environment: pypi" in workflow
    assert workflow.count("id-token: write") == 2
    assert "repository-url: https://test.pypi.org/legacy/" in workflow
    assert "password:" not in workflow
    assert "api-token:" not in workflow
    assert workflow.count("github.event_name == 'workflow_dispatch'") == 3


def test_sdk_release_builds_once_and_preserves_the_pypi_release_hold() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "name: zeroth-sdk-dist" in workflow
    assert "uv build packaging/sdk --out-dir dist/zeroth-sdk" in workflow
    assert "tool.zeroth.release.publish" in workflow
    assert "SDK production publishing remains blocked" in workflow
    assert "needs: [build]" in workflow


def test_sdk_readme_documents_truthful_pypi_and_testpypi_install_paths() -> None:
    readme = SDK_README.read_text(encoding="utf-8")
    metadata = tomllib.loads(SDK_PYPROJECT.read_text(encoding="utf-8"))
    version = metadata["project"]["version"]

    assert readme.startswith("# zeroth-sdk\n")
    assert "pip install zeroth-sdk" in readme
    assert "https://test.pypi.org/simple/" in readme
    assert "https://pypi.org/simple/" in readme
    assert f"zeroth-sdk=={version}" in readme
    assert "release-zeroth-sdk.yml" in readme
    assert "Trusted Publishing" in readme
    assert "No SDK release is currently available on either index" in readme
    assert "tool.zeroth.release.publish = false" in readme


def test_root_readme_shows_live_pypi_and_testpypi_package_checks() -> None:
    readme = ROOT_README.read_text(encoding="utf-8")

    assert "label=PyPI%20check" in readme
    assert "label=TestPyPI%20check" in readme
    assert readme.count("release-zeroth-sdk.yml") >= 4
