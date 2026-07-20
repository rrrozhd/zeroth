"""Data models for parallel fan-out/fan-in execution.

Contains the configuration, context, result, and tracking objects used by
the ParallelExecutor to manage concurrent branch execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Re-exported as zeroth.runtime.parallel API: the authored fan-out configuration
# is graph contract vocabulary; the runtime execution objects stay here.
from zeroth.contracts.graph.models import ParallelConfig as ParallelConfig
from zeroth.runtime.parallel.errors import ParallelStepLimitError


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
