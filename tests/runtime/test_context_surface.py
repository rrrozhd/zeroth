"""Canonical import surface for the runtime context package.

Non-golden boundary tests for the Task 14 context-window move: the canonical
``zeroth.runtime.context`` package must publish the same objects the legacy
``zeroth.core.context_window`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


EXPORTS = (
    "CompactionError",
    "CompactionResult",
    "CompactionState",
    "CompactionStrategy",
    "ContextWindowError",
    "ContextWindowSettings",
    "ContextWindowTracker",
    "LLMSummarizationStrategy",
    "ObservationMaskingStrategy",
    "TokenCountError",
    "TruncationStrategy",
)


def test_context_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import context_window as legacy
    from zeroth.runtime import context as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("errors", ("CompactionError", "ContextWindowError", "TokenCountError")),
        ("models", ("CompactionResult", "CompactionState", "ContextWindowSettings")),
        (
            "strategies",
            (
                "CompactionStrategy",
                "LLMSummarizationStrategy",
                "ObservationMaskingStrategy",
                "TruncationStrategy",
            ),
        ),
        ("tracker", ("ContextWindowTracker",)),
    ],
)
def test_context_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.context_window.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.runtime.context.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.runtime.context", "zeroth.core.context_window"),
        ("zeroth.core.context_window", "zeroth.runtime.context"),
    ],
)
def test_context_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
