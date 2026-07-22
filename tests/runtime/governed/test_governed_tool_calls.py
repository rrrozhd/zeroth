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

import pytest

NAMES = (
    "GovernedToolCallLoop",
    "NormalizedToolCall",
    "build_tool_message",
    "extract_tool_calls",
)


def test_tool_calls_are_the_same_surface_through_both_paths() -> None:
    import importlib

    legacy = importlib.import_module("zeroth.core.governed.integrations.tool_calls")
    canonical = importlib.import_module("zeroth.runtime.agents.tooling.tool_calls")

    for name in NAMES:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "zeroth.runtime.agents.tooling.tool_calls",
            "zeroth.core.governed.integrations.tool_calls",
        ),
        (
            "zeroth.core.governed.integrations.tool_calls",
            "zeroth.runtime.agents.tooling.tool_calls",
        ),
    ],
)
def test_tool_calls_cold_import_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
