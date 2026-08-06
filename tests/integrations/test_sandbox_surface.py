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


def test_sandbox_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.integrations.sandbox as canonical

    expected = {
        "app",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.integrations.sandbox no longer publishes: {missing}"


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
def test_sandbox_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    """Each relocated submodule still exports the names it was pinned for."""
    import importlib

    canonical_module = importlib.import_module(f"zeroth.integrations.sandbox.{module_name}")

    missing = sorted(name for name in names if not hasattr(canonical_module, name))
    assert not missing, f"{module_name} no longer publishes: {missing}"


def test_sandbox_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.integrations.sandbox"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
