"""Legacy import path for the runtime parallel package.

Parallel fan-out/fan-in execution lives in :mod:`zeroth.runtime.parallel`;
this package republishes the same objects for compatibility. Import from
the canonical location instead (see docs/backend-import-migration.md).
"""

from zeroth.runtime.parallel import (
    BranchContext,
    BranchError,
    BranchResult,
    FanInResult,
    FanOutValidationError,
    GlobalStepTracker,
    ParallelConfig,
    ParallelExecutionError,
    ParallelExecutor,
    ParallelStepLimitError,
)

__all__ = [
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
]
