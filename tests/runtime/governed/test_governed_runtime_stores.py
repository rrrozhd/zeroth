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
    ("legacy_module", "canonical_module", "names"),
    [
        (
            "zeroth.core.governed.runtime.interrupts",
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
            "zeroth.core.governed.runtime.run_store",
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
def test_governed_runtime_stores_are_the_same_objects(
    legacy_module: str, canonical_module: str, names: tuple[str, ...]
) -> None:
    legacy = importlib.import_module(legacy_module)  # noqa: F841
    canonical = importlib.import_module(canonical_module)

    for name in names:
        assert hasattr(canonical, name), name


def test_governed_runtime_package_exports_stay_available() -> None:
    from zeroth.core.governed import runtime as legacy
    from zeroth.runtime.orchestration.interrupts import RedisInterruptStore
    from zeroth.runtime.orchestration.run_store import RedisRunStore

    assert legacy.RedisInterruptStore is RedisInterruptStore
    assert legacy.RedisRunStore is RedisRunStore


@pytest.mark.parametrize(
    ("first", "second"),
    [],
)
def test_governed_runtime_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
