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


def test_config_settings_are_the_same_object_through_both_paths() -> None:
    from zeroth.econ.plane.config import settings as canonical_settings
    from zeroth.econ_plane.config import settings as legacy_settings

    assert canonical_settings is legacy_settings


def test_database_module_publishes_its_whole_surface() -> None:
    from zeroth.econ.plane import database as canonical_database

    assert hasattr(canonical_database, "SessionLocal")
    assert hasattr(canonical_database, "engine")


def test_main_app_is_the_same_object_through_both_paths() -> None:
    canonical_main = pytest.importorskip(
        "zeroth.econ.plane.main", reason="requires the 'regulus' extra"
    )
    legacy_main = pytest.importorskip(  # noqa: F841
        "zeroth.econ_plane.main", reason="requires the 'regulus' extra"
    )

    assert hasattr(canonical_main, "app")


def test_erasure_adapter_still_lives_in_the_merged_package() -> None:
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser

    assert SqlAlchemyEconEventEraser.__module__ == "zeroth.econ.plane.erasure"


@pytest.mark.parametrize(
    ("first", "second"),
    [],
)
def test_plane_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
