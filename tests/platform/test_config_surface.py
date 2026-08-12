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
from typing import get_args, get_type_hints

import pytest


def test_config_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.platform.config as canonical

    expected = {
        "ZerothSettings",
        "get_settings",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.platform.config no longer publishes: {missing}"


def test_the_composed_settings_sections_are_platform_owned() -> None:
    """The econ and http settings sections are defined in platform config.

    ``ZerothSettings`` composes ``RegulusSettings`` and ``HttpClientSettings``
    as fields, so their definitions must sit below every other domain; the
    legacy ``zeroth.core.econ.models`` and ``zeroth.core.http.models`` paths
    republish the same class objects.
    """
    from zeroth.econ.analytics import models as legacy_econ_models
    from zeroth.integrations.http import models as legacy_http_models
    from zeroth.platform.config.models import HttpClientSettings, RegulusSettings

    assert RegulusSettings is legacy_econ_models.RegulusSettings
    assert HttpClientSettings is legacy_http_models.HttpClientSettings


def test_config_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.platform.config"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_exact_dispatch_settings_have_closed_types() -> None:
    from zeroth.platform.artifacts.models import ArtifactStoreSettings
    from zeroth.platform.config.settings import (
        ProvenanceSigningSettings,
        RedisSettings,
        SandboxSettings,
        SecretsSettings,
    )

    expected = {
        (RedisSettings, "mode"): {"local", "disabled"},
        (SandboxSettings, "backend"): {"local", "docker", "auto", "sidecar"},
        (SecretsSettings, "backend"): {"env", "vault"},
        (ProvenanceSigningSettings, "mode"): {"env", "kms", "off"},
        (ArtifactStoreSettings, "backend"): {"filesystem", "redis"},
    }
    for (model, field), choices in expected.items():
        assert set(get_args(get_type_hints(model)[field])) == choices


def test_postgres_requires_dsn_during_settings_validation() -> None:
    from pydantic import ValidationError

    from zeroth.platform.config.settings import DatabaseSettings

    with pytest.raises(ValidationError, match="postgres backend requires postgres_dsn"):
        DatabaseSettings(backend="postgres")
