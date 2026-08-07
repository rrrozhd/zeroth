"""Equivalence pins for the governed tool-call helpers consolidation.

Task 15 moves the vendored ``zeroth.core.governed.integrations.tool_calls``
provider helpers into the maintained agent tooling package: they are pure
parsing/loop logic the runtime calls at dispatch time, so they are
runtime-owned (the Task 14 ``governed/tools`` precedent), not integrations.
These tests pin, red first, that the canonical
``zeroth.runtime.agents.tooling.tool_calls`` module publishes the very same
objects the legacy vendored path keeps republishing.
"""

from __future__ import annotations

import subprocess
import sys

NAMES = (
    "GovernedToolCallLoop",
    "NormalizedToolCall",
    "build_tool_message",
    "extract_tool_calls",
)


def test_tool_calls_publishes_its_whole_surface() -> None:
    """The merged module still exports every name the vendored one did.

    This compared the canonical module against the vendored
    ``zeroth.core.governed.integrations.tool_calls`` republisher, which ZER-25
    removed; the surface it pinned is asserted directly.
    """
    import importlib

    canonical = importlib.import_module("zeroth.runtime.agents.tooling.tool_calls")

    missing = sorted(name for name in NAMES if not hasattr(canonical, name))
    assert not missing, f"tool_calls no longer publishes: {missing}"


def test_tool_calls_cold_imports() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.runtime.agents.tooling.tool_calls"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"tool_calls is not cold-importable:\n{result.stderr}"
