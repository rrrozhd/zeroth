"""Canonical import surface for the runtime parallel package.

Non-golden boundary tests for the Task 14 parallel move: the canonical
``zeroth.runtime.parallel`` package must publish the same objects the legacy
``zeroth.core.parallel`` path keeps republishing, and both packages must
stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

EXPORTS = (
    "BranchContext",
    "BranchError",
    "BranchResult",
    "FanInResult",
    "FanOutValidationError",
    "GlobalStepTracker",
    "ParallelConfig",
    "ParallelExecutionError",
    "ParallelExecutor",
    "ParallelStepLimitError",
)


def test_parallel_publishes_its_whole_surface() -> None:
    from zeroth.runtime import parallel as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        (
            "errors",
            (
                "BranchApprovalPauseSignal",
                "BranchError",
                "FanOutValidationError",
                "MergeStrategyError",
                "MergeStrategyValidationError",
                "ParallelExecutionError",
                "ParallelStepLimitError",
                "ReducerRefValidationError",
            ),
        ),
        ("executor", ("ParallelExecutor",)),
        (
            "models",
            (
                "BranchContext",
                "BranchResult",
                "FanInResult",
                "GlobalStepTracker",
                "ParallelConfig",
            ),
        ),
        ("reducers", ("dispatch_strategy", "resolve_reducer_ref")),
    ],
)
def test_parallel_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    canonical_module = importlib.import_module(f"zeroth.runtime.parallel.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


def test_parallel_config_remains_the_contract_owned_model() -> None:
    from zeroth.contracts.graph.models import ParallelConfig as ContractParallelConfig
    from zeroth.runtime.parallel.models import ParallelConfig as RuntimeParallelConfig

    assert RuntimeParallelConfig is ContractParallelConfig


def test_parallel_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.runtime.parallel"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
