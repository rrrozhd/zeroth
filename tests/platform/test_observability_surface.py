"""Canonical import surface for the platform observability package.

Non-golden boundary tests for the Task 11 observability move: the canonical
``zeroth.platform.observability`` package must publish the same objects the
legacy ``zeroth.core.observability`` path keeps republishing, and both
packages must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_observability_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import observability as legacy
    from zeroth.platform import observability as canonical

    assert canonical.MetricsCollector is legacy.MetricsCollector
    assert canonical.configure_tracing is legacy.configure_tracing
    assert canonical.get_correlation_id is legacy.get_correlation_id
    assert canonical.new_correlation_id is legacy.new_correlation_id
    assert canonical.set_correlation_id is legacy.set_correlation_id
    assert canonical.start_span is legacy.start_span


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.observability", "zeroth.core.observability"),
        ("zeroth.core.observability", "zeroth.platform.observability"),
    ],
)
def test_observability_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
