"""The dangling SDK protocol cannot become an accidental stable release."""

from __future__ import annotations

import tomllib
from pathlib import Path


SDK_PYPROJECT = Path(__file__).parents[2] / "packaging" / "sdk" / "pyproject.toml"


def test_sdk_is_machine_marked_as_release_blocked() -> None:
    metadata = tomllib.loads(SDK_PYPROJECT.read_text(encoding="utf-8"))

    assert metadata["project"]["version"].endswith(".dev0")
    assert metadata["tool"]["zeroth"]["release"]["publish"] is False
    reason = metadata["tool"]["zeroth"]["release"]["reason"]
    assert "hosted endpoint" in reason
    assert "No in-repo server" not in reason
