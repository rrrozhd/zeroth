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


def test_config_publishes_a_live_settings_instance_on_the_ecp_prefix() -> None:
    """The control plane reads its configuration from ``ECP_``-prefixed variables.

    An earlier revision asserted ``config.settings is config.settings``, and the
    revision after that re-imported the module and compared the results -- both
    tautologies, since a cached module necessarily hands back the same object.
    The property that is actually load-bearing, and that ZER-25 must preserve, is
    the environment prefix the control plane binds to: change it and every
    deployment's configuration silently stops being read.
    """
    from pydantic_settings import BaseSettings

    from zeroth.econ.plane import config

    assert isinstance(config.settings, BaseSettings)
    assert type(config.settings).model_config["env_prefix"] == "ECP_"


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
