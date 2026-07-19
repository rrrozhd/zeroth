"""Canonical import surface for the platform config package.

Non-golden boundary tests for the Task 11 config move: the canonical
``zeroth.platform.config`` package must publish the same objects the legacy
``zeroth.core.config`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order (the warm-cache
suite cannot see partial-initialization cycles; see
tests/architecture/test_import_layering.py).
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_config_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import config as legacy
    from zeroth.platform import config as canonical

    assert canonical.ZerothSettings is legacy.ZerothSettings
    assert canonical.get_settings is legacy.get_settings


def test_the_composed_settings_sections_are_platform_owned() -> None:
    """The econ and http settings sections are defined in platform config.

    ``ZerothSettings`` composes ``RegulusSettings`` and ``HttpClientSettings``
    as fields, so their definitions must sit below every other domain; the
    legacy ``zeroth.core.econ.models`` and ``zeroth.core.http.models`` paths
    republish the same class objects.
    """
    from zeroth.core.econ import models as legacy_econ_models
    from zeroth.core.http import models as legacy_http_models
    from zeroth.platform.config.models import HttpClientSettings, RegulusSettings

    assert RegulusSettings is legacy_econ_models.RegulusSettings
    assert HttpClientSettings is legacy_http_models.HttpClientSettings


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.config", "zeroth.core.config"),
        ("zeroth.core.config", "zeroth.platform.config"),
    ],
)
def test_config_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
