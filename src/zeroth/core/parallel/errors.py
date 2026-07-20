"""Legacy import path for :mod:`zeroth.runtime.parallel.errors`."""

from zeroth.runtime.parallel.errors import (
    BranchApprovalPauseSignal,
    BranchError,
    FanOutValidationError,
    MergeStrategyError,
    MergeStrategyValidationError,
    ParallelExecutionError,
    ParallelStepLimitError,
    ReducerRefValidationError,
)

__all__ = [
    "BranchApprovalPauseSignal",
    "BranchError",
    "FanOutValidationError",
    "MergeStrategyError",
    "MergeStrategyValidationError",
    "ParallelExecutionError",
    "ParallelStepLimitError",
    "ReducerRefValidationError",
]
