"""Landing pages distinguish the installable client from platform and hosting."""

from pathlib import Path
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", ["README.md", "docs/index.md"])
def test_landing_page_has_explicit_package_and_hosting_boundaries(path: str) -> None:
    text = (ROOT / path).read_text()
    normalized = " ".join(text.lower().split())
    metadata = tomllib.loads((ROOT / "packaging/sdk/pyproject.toml").read_text())
    version = metadata["project"]["version"]
    assert f'pip install "zeroth-sdk=={version}"' in text
    assert "experimental" in normalized
    assert "base_url" in text
    for package in ("zeroth-sdk", "zeroth-platform", "zeroth-core", "zeroth-console"):
        assert package in text
    assert "not yet available" in normalized
    for obsolete in ("release-blocked", "temporarily unavailable", "preserved platform"):
        assert obsolete not in normalized
    for capability in ("orchestration", "governance", "economic"):
        assert capability in normalized


def test_readme_maps_all_three_companion_package_sources() -> None:
    text = (ROOT / "README.md").read_text()
    for path in ("packaging/sdk/", "packaging/console/", "packaging/core-compat/"):
        assert path in text
        assert (ROOT / path).is_dir()


def test_install_tutorial_does_not_send_readers_to_unpublished_platform() -> None:
    text = (ROOT / "docs/tutorials/getting-started/01-install.md").read_text()
    assert "pip install zeroth-platform" not in text
    assert "uv add zeroth-platform" not in text
    assert "uv sync --extra regulus" in text
