"""Parallel fan-out/fan-in execution for governed workflows.

This package provides the data models, error hierarchy, and execution engine
for splitting a node's output into concurrent branches, running them in
parallel via asyncio, and collecting the results with deterministic ordering.

Public API
----------
Models:
    ParallelConfig, BranchContext, BranchResult, FanInResult, GlobalStepTracker

Errors:
    ParallelExecutionError, BranchError, FanOutValidationError, ParallelStepLimitError

Executor:
    ParallelExecutor
"""

from zeroth.runtime.parallel.errors import (
    BranchError,
    FanOutValidationError,
    MultipleBranchPauseError,
    ParallelExecutionError,
    ParallelStepLimitError,
)
from zeroth.runtime.parallel.executor import ParallelExecutor
from zeroth.runtime.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    GlobalStepTracker,
    ParallelConfig,
)

__all__ = [
    "BranchContext",
    "BranchError",
    "BranchResult",
    "FanInResult",
    "FanOutValidationError",
    "GlobalStepTracker",
    "MultipleBranchPauseError",
    "ParallelConfig",
    "ParallelExecutionError",
    "ParallelExecutor",
    "ParallelStepLimitError",
]
