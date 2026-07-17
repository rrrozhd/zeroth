"""Data models for parallel fan-out/fan-in execution.

Contains the configuration, context, result, and tracking objects used by
the ParallelExecutor to manage concurrent branch execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.core.parallel.errors import ParallelStepLimitError


class ParallelConfig(BaseModel):
    """Configuration for parallel fan-out on a node.

    Specifies how to split output into branches, how to merge results,
    what to do when a branch fails, and an optional cap on branch count.
    """

    model_config = ConfigDict(extra="forbid")

    split_path: str
    """Dot-path to the list in the node's output that should be split."""

    merge_strategy: Literal["collect", "reduce", "merge", "custom"] = "collect"
    """How branch outputs are combined (D-04 literal):
    'collect' gathers into a list, 'reduce' applies the built-in
    last-wins fold, 'merge' shallow-merges dicts in branch order, and
    'custom' applies a user-supplied dotted-path reducer."""

    reducer_ref: str | None = None
    """Dotted import path to a user-supplied reducer callable. Only valid
    with ``merge_strategy='custom'`` (D-04). The callable must accept two
    positional arguments ``(accumulator, next_value)`` and return the new
    accumulator. Rejected at publish time if the path cannot be resolved
    to a callable."""

    fail_mode: Literal["fail_fast", "best_effort"] = "fail_fast"
    """Behavior on branch failure: 'fail_fast' cancels remaining branches
    on the first error, 'best_effort' runs all branches and collects errors."""

    max_branches: int | None = Field(default=None, ge=1)
    """Optional cap on the number of parallel branches. None means unlimited."""

    @model_validator(mode="after")
    def _validate_reducer_ref_consistency(self) -> ParallelConfig:
        """Enforce D-04 literal: only ``custom`` requires ``reducer_ref``.

        ``reduce`` uses a built-in default fold and MUST NOT carry a
        ``reducer_ref``. All other strategies (``collect``, ``merge``) also
        reject ``reducer_ref``. This keeps ``reduce`` and ``custom``
        semantically distinct.
        """
        if self.merge_strategy == "custom" and not self.reducer_ref:
            raise ValueError("merge_strategy='custom' requires reducer_ref to be set")
        if self.merge_strategy != "custom" and self.reducer_ref is not None:
            raise ValueError(
                "reducer_ref is only valid with merge_strategy='custom', "
                f"got merge_strategy={self.merge_strategy!r}"
            )
        return self


class JoinConfig(BaseModel):
    """Merge policy for a sequential *join* (convergent) node (B9).

    A convergent node reached by more than one non-tool control-flow edge that
    is *not* a ``parallel_config`` fan-in needs to declare how the payloads of
    its delivered inbound edges combine when >1 deliver in the same iteration.
    This reuses the exact ``merge_strategy``/``reducer_ref`` vocabulary as
    :class:`ParallelConfig` and dispatches through the same
    ``parallel.reducers.dispatch_strategy`` registry — the join subsystem does
    NOT reinvent merge semantics.

    Unlike ``ParallelConfig`` there is no ``split_path``/``fail_mode``/
    ``max_branches``: a join has no fan-out list to split and no per-branch
    failure handling; it simply reduces the ordered list of delivered inbound
    payloads.

    Default ``merge_strategy='merge'`` (shallow dict merge, last delivered edge
    wins on key conflict) — the least-surprise behaviour for a plain diamond.
    """

    model_config = ConfigDict(extra="forbid")

    merge_strategy: Literal["collect", "reduce", "merge", "custom"] = "merge"
    """How delivered inbound payloads combine (same vocabulary as
    ``ParallelConfig``): 'merge' shallow-merges dicts in inbound-edge order,
    'reduce' applies the built-in last-wins fold, 'collect' gathers into a list,
    and 'custom' applies a user-supplied dotted-path reducer."""

    reducer_ref: str | None = None
    """Dotted import path to a user-supplied reducer callable. Only valid with
    ``merge_strategy='custom'`` (mirrors ``ParallelConfig``)."""

    @model_validator(mode="after")
    def _validate_reducer_ref_consistency(self) -> JoinConfig:
        """Only ``custom`` may (and must) carry a ``reducer_ref`` (mirrors ParallelConfig)."""
        if self.merge_strategy == "custom" and not self.reducer_ref:
            raise ValueError("merge_strategy='custom' requires reducer_ref to be set")
        if self.merge_strategy != "custom" and self.reducer_ref is not None:
            raise ValueError(
                "reducer_ref is only valid with merge_strategy='custom', "
                f"got merge_strategy={self.merge_strategy!r}"
            )
        return self


@dataclass(slots=True)
class BranchContext:
    """Isolated execution context for a single parallel branch.

    Each branch gets its own visit counts, execution history, and audit
    trail so that branches never share mutable Run state (D-05).
    """

    branch_index: int
    branch_id: str
    input_payload: dict[str, Any]
    node_visit_counts: dict[str, int] = field(default_factory=dict)
    execution_history: list[Any] = field(default_factory=list)
    audit_refs: list[str] = field(default_factory=list)
    condition_results: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BranchResult:
    """The outcome of executing a single parallel branch.

    On success, output contains the branch's result dict and error is None.
    On failure, output is None and error contains the error message.
    """

    branch_index: int
    output: dict[str, Any] | None
    error: str | None = None
    audit_refs: list[str] = field(default_factory=list)
    execution_history: list[Any] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass(slots=True)
class FanInResult:
    """Aggregated result from all parallel branches after synchronization.

    Contains the ordered list of branch results, the merged output dict,
    and aggregate cost/step metrics.

    When ``pause_state`` is not None, a branch raised
    ``BranchApprovalPauseSignal`` mid-execution. In that case
    ``results``/``merged_output`` should NOT be consumed by downstream
    logic — the parent orchestrator must instead stash
    ``pending_parallel_subgraph`` metadata carrying the completed,
    paused, and cancelled branch state so resume can reconstruct the
    fan-in byte-identically (D-11 literal).

    Expected ``pause_state`` key set (documented, not enforced):

    * ``"paused"``: dict with ``branch_index``, ``child_run_id``,
      ``graph_ref``, ``version``, ``node_id``, ``branch_context``.
    * ``"completed_branch_results"``: ``list[BranchResult]`` finished
      BEFORE the pause — reused as-is on resume.
    * ``"cancelled_branch_contexts"``: ``list[BranchContext]`` in-flight
      when the pause fired — recorded as None-output BranchResults on
      resume per D-19.
    """

    results: list[BranchResult]
    merged_output: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_steps: int = 0
    pause_state: dict[str, Any] | None = None


class GlobalStepTracker:
    """Async-safe counter that enforces a global step limit across branches.

    Uses an asyncio.Lock to ensure that concurrent branches cannot exceed
    the configured max_total_steps limit (D-06).
    """

    def __init__(self, current_steps: int, max_steps: int) -> None:
        self._count = current_steps
        self._max = max_steps
        self._lock = asyncio.Lock()

    async def increment(self) -> None:
        """Increment the step counter. Raises ParallelStepLimitError if at limit."""
        async with self._lock:
            if self._count >= self._max:
                msg = f"global step limit reached: {self._count} >= {self._max}"
                raise ParallelStepLimitError(msg)
            self._count += 1

    @property
    def count(self) -> int:
        """Current step count."""
        return self._count
