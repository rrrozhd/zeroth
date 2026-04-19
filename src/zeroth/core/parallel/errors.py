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


class MergeStrategyError(ParallelExecutionError):
    """Runtime failure during fan-in reduction.

    Raised when a merge strategy cannot produce a valid reduced value at
    runtime, such as ``merge`` encountering a non-dict branch output, or a
    user-supplied reducer raising an exception.
    """


class MergeStrategyValidationError(ParallelExecutionError):
    """Publish-time rejection of invalid merge strategy configuration.

    Raised by ``GraphValidator`` when a ``ParallelConfig`` on a node is
    inconsistent, misconfigured, or incompatible with the node's output
    contract. Blocks DRAFT -> PUBLISHED transition.
    """


class ReducerRefValidationError(MergeStrategyValidationError):
    """A ``reducer_ref`` string failed regex / importlib / callable check.

    Raised when a dotted import path is malformed, points to a missing
    module or attribute, or resolves to a non-callable object.
    """


class BranchApprovalPauseSignal(BaseException):
    """Pause propagation from a subgraph child inside a parallel branch.

    Subclasses ``BaseException`` (NOT ``Exception``) so:

    * ``asyncio.gather(return_exceptions=True)`` does NOT swallow it —
      it escapes as a raised ``BaseException`` and the outer
      fail-fast/best-effort path can catch it explicitly.
    * Cancellation of sibling branches propagates through the task
      group even in best-effort mode.

    Carries the metadata needed by ``_execute_parallel_fan_out`` to
    stash ``pending_parallel_subgraph`` on the parent Run so the paused
    branch can be resumed later via ``SubgraphExecutor.resume`` without
    re-executing any sibling.
    """

    def __init__(
        self,
        *,
        branch_index: int,
        child_run_id: str,
        graph_ref: str,
        version: int | None,
        node_id: str,
    ) -> None:
        super().__init__(
            f"branch {branch_index} paused on subgraph "
            f"{graph_ref}@{version} (child_run={child_run_id})"
        )
        self.branch_index = branch_index
        self.child_run_id = child_run_id
        self.graph_ref = graph_ref
        self.version = version
        self.node_id = node_id
