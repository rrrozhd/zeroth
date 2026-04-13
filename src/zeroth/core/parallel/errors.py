"""Error hierarchy for parallel fan-out/fan-in execution.

These exceptions cover the different failure modes that can occur during
parallel branch execution: validation errors at fan-out time, individual
branch failures, aggregate execution errors, and step limit violations.
"""

from __future__ import annotations


class ParallelExecutionError(RuntimeError):
    """Base error for parallel execution failures.

    Raised when the overall parallel execution encounters a problem that
    prevents it from completing, such as a fail-fast cancellation.
    """


class BranchError(ParallelExecutionError):
    """A single branch failed during parallel execution.

    Carries the branch index and the original exception so callers can
    identify which branch failed and why.
    """

    def __init__(self, branch_index: int, original: BaseException) -> None:
        self.branch_index = branch_index
        self.original = original
        super().__init__(f"branch {branch_index} failed: {original}")


class FanOutValidationError(ParallelExecutionError):
    """Invalid fan-out configuration or input data.

    Raised before any branches are spawned when something is wrong with
    the split_path, the data at that path, or the node type.
    """


class ParallelStepLimitError(ParallelExecutionError):
    """Global step limit exceeded across all parallel branches.

    The sum of steps taken across all concurrent branches has reached or
    exceeded the maximum allowed by ExecutionSettings.max_total_steps.
    """
