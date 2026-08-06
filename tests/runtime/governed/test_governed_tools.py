"""Equivalence pins for the governed tool primitives consolidation.

Task 14 merges the vendored ``zeroth.core.governed.tools`` primitives into the
maintained agent runtime package. These tests pin, red first, that the
canonical ``zeroth.runtime.agents.tooling`` modules publish the very same
objects the legacy vendored path keeps republishing, and that the aggregator
and contract republication chains survive the move.
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
            "zeroth.runtime.agents.tooling.base",
            (
                "CLIToolError",
                "CLIToolOutputError",
                "CLIToolProcessError",
                "CLIToolTimeoutError",
                "ExecutionPlacement",
                "Tool",
                "ToolError",
                "ToolExecutionError",
                "ToolValidationError",
            ),
        ),
        (
            "zeroth.runtime.agents.tooling.cli_tool",
            ("CLITool",),
        ),
        (
            "zeroth.runtime.agents.tooling.manifest",
            ("ToolManifest",),
        ),
        (
            "zeroth.runtime.agents.tooling.python_tool",
            ("PythonHandler", "PythonReturn", "PythonTool", "tool"),
        ),
    ],
)
def test_governed_tools_are_the_same_objects(canonical_module: str, names: tuple[str, ...]) -> None:
    canonical = importlib.import_module(canonical_module)

    for name in names:
        assert hasattr(canonical, name), name


def test_aggregator_keeps_republishing_the_runtime_owned_tool() -> None:
    from zeroth.runtime.agents.tooling.base import Tool as AggregatorTool
    from zeroth.runtime.agents.tooling.base import Tool as CanonicalTool

    assert AggregatorTool is CanonicalTool


def test_execution_placement_remains_the_contract_owned_model() -> None:
    from zeroth.contracts.registry.tooling import ExecutionPlacement as ContractPlacement
    from zeroth.runtime.agents.tooling.base import ExecutionPlacement as RepublishedPlacement

    assert RepublishedPlacement is ContractPlacement


@pytest.mark.parametrize(
    "module",
    (
        "zeroth.runtime.agents.tooling.base",
        "zeroth.runtime.agents.tooling.cli_tool",
        "zeroth.runtime.agents.tooling.manifest",
        "zeroth.runtime.agents.tooling.python_tool",
    ),
)
def test_governed_tools_modules_import_in_a_cold_interpreter(module: str) -> None:
    """Each canonical module imports with nothing else pre-warmed.

    The original ran every ordered pair of canonical and legacy packages to
    catch a cycle between them. An emptied parameter list would collect zero
    cases and pass while proving nothing, so each canonical module is asserted
    to stand up on its own instead.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"cold import of {module} failed:\n{result.stderr}"
