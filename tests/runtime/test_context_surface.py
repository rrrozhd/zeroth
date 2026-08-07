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


def test_context_publishes_its_whole_surface() -> None:
    from zeroth.runtime import context as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


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
def test_context_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    canonical_module = importlib.import_module(f"zeroth.runtime.context.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


def test_context_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.runtime.context"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
