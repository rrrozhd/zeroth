"""The first public SDK release remains explicitly and machine-authorized."""

from __future__ import annotations

import tomllib
from pathlib import Path


SDK_PYPROJECT = Path(__file__).parents[2] / "packaging" / "sdk" / "pyproject.toml"


def test_sdk_is_machine_marked_for_the_public_self_hosted_release() -> None:
    metadata = tomllib.loads(SDK_PYPROJECT.read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == "0.1.0a1"
    assert metadata["project"]["classifiers"][0] == "Development Status :: 3 - Alpha"
    assert metadata["tool"]["zeroth"]["release"]["publish"] is True
    reason = metadata["tool"]["zeroth"]["release"]["reason"]
    assert "self-hosted" in reason
    assert "experimental" in reason.lower()
