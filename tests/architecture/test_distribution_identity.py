"""Canonical distribution names and compatibility-package boundaries."""

from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_full_product_is_distributed_as_zeroth_platform() -> None:
    metadata = _toml(ROOT / "pyproject.toml")["project"]

    assert metadata["name"] == "zeroth-platform"
    assert metadata["scripts"]["zeroth"] == "zeroth.service.cli:main"
    assert metadata["scripts"]["zeroth-core"] == "zeroth.service.cli:main"
    assert all(
        requirement.startswith("zeroth-platform[")
        for requirement in metadata["optional-dependencies"]["all"]
    )


def test_console_is_an_optional_zeroth_platform_surface() -> None:
    metadata = _toml(ROOT / "packaging/console/pyproject.toml")["project"]

    assert "zeroth-platform" in metadata["description"]
    assert "zeroth-core" not in metadata["description"]


def test_zeroth_core_is_an_exact_version_compatibility_package() -> None:
    platform = _toml(ROOT / "pyproject.toml")["project"]
    compatibility = _toml(ROOT / "packaging/core-compat/pyproject.toml")["project"]

    assert compatibility["name"] == "zeroth-core"
    assert compatibility["version"] == platform["version"]
    assert compatibility["dependencies"] == [f"zeroth-platform=={platform['version']}"]
