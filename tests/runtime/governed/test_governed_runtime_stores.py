"""Equivalence pins for the governed runtime stores consolidation.

Task 14 merges the vendored ``zeroth.core.governed.runtime`` interrupt and run
stores into the maintained runtime orchestration package. These tests pin, red
first, that the canonical modules publish the very same objects the legacy
vendored path keeps republishing.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("canonical_module", "names"),
    [
        (
            "zeroth.runtime.orchestration.interrupts",
            (
                "InMemoryInterruptStore",
                "InterruptManager",
                "InterruptRequest",
                "InterruptResolution",
                "InterruptStore",
                "RedisInterruptStore",
            ),
        ),
        (
            "zeroth.runtime.orchestration.run_store",
            (
                "InMemoryRunStore",
                "RedisRunStore",
                "RunStore",
                "StateConcurrencyError",
                "ThreadAwareRunStore",
            ),
        ),
    ],
)
def test_governed_runtime_stores_publish_their_names(
    canonical_module: str, names: tuple[str, ...]
) -> None:
    canonical = importlib.import_module(canonical_module)

    for name in names:
        assert hasattr(canonical, name), name


def test_governed_runtime_package_exports_stay_available() -> None:
    """The merged stores are reachable from the runtime orchestration package.

    This compared them against the vendored ``zeroth.core.governed.runtime``
    republisher, which ZER-25 removed; availability at the canonical location
    is what the assertion was protecting.
    """
    from zeroth.runtime.orchestration.interrupts import RedisInterruptStore
    from zeroth.runtime.orchestration.run_store import RedisRunStore

    assert RedisInterruptStore.__module__ == "zeroth.runtime.orchestration.interrupts"
    assert RedisRunStore.__module__ == "zeroth.runtime.orchestration.run_store"


@pytest.mark.parametrize(
    "module", ("zeroth.runtime.orchestration.interrupts", "zeroth.runtime.orchestration.run_store")
)
def test_governed_runtime_modules_import_in_a_cold_interpreter(module: str) -> None:
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
