"""Canonical import surface for the economic control plane package.

Non-golden boundary tests for the Task 14 econ_plane move: the canonical
``zeroth.econ.plane`` package must publish the same objects the legacy
``zeroth.econ_plane`` path keeps republishing, the merged package must keep
the erasure adapter that already lived there, and both paths must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_config_exposes_a_single_settings_object() -> None:
    """The control plane's settings singleton is published once.

    This compared the canonical object against the ``zeroth.econ_plane``
    republisher, which ZER-25 removed; the property it pinned -- that the
    module exposes one settings instance -- is asserted directly.
    """
    from zeroth.econ.plane import config

    assert config.settings is config.settings
    assert not isinstance(config.settings, type)


def test_database_module_publishes_its_whole_surface() -> None:
    from zeroth.econ.plane import database as canonical_database

    assert hasattr(canonical_database, "SessionLocal")
    assert hasattr(canonical_database, "engine")


def test_main_publishes_the_application() -> None:
    canonical_main = pytest.importorskip(
        "zeroth.econ.plane.main", reason="requires the 'regulus' extra"
    )

    assert hasattr(canonical_main, "app")


def test_erasure_adapter_still_lives_in_the_merged_package() -> None:
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser

    assert SqlAlchemyEconEventEraser.__module__ == "zeroth.econ.plane.erasure"


@pytest.mark.parametrize("module", ("zeroth.econ.plane.config", "zeroth.econ.plane.database"))
def test_plane_modules_import_in_a_cold_interpreter(module: str) -> None:
    """Each canonical module imports with nothing else pre-warmed.

    The original ran every ordered pair of canonical and legacy packages to
    catch a cycle between them. With the legacy packages gone, an emptied
    parameter list would collect zero cases and pass while proving nothing --
    so it asserts each canonical module stands up on its own instead.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"cold import of {module} failed:\n{result.stderr}"
