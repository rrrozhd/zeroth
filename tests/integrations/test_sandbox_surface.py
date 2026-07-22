"""Canonical import surface for the sandbox integrations package.

Non-golden boundary tests for the Task 15 sandbox-sidecar move: the
canonical ``zeroth.integrations.sandbox`` package must publish the same
objects the legacy ``zeroth.core.sandbox_sidecar`` path keeps republishing,
and both packages must stay cold-importable from a fresh interpreter in
either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_sandbox_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import sandbox_sidecar as legacy
    from zeroth.integrations import sandbox as canonical

    assert canonical.app is legacy.app


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("app", ("app",)),
        ("executor", ("SidecarExecutor",)),
        (
            "models",
            (
                "SidecarExecuteRequest",
                "SidecarExecuteResponse",
                "SidecarHealthResponse",
                "SidecarStatusResponse",
            ),
        ),
    ],
)
def test_sandbox_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.sandbox_sidecar.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.integrations.sandbox.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.integrations.sandbox", "zeroth.core.sandbox_sidecar"),
        ("zeroth.core.sandbox_sidecar", "zeroth.integrations.sandbox"),
    ],
)
def test_sandbox_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
