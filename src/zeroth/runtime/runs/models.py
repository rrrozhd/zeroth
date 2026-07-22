"""Canonical run and thread domain models.

The runtime owns the shape of a run: its identity, status, execution history,
condition results, failure state, and the thread that groups runs together.
These models are published here so runtime code depends on the run domain
rather than on whichever package happens to persist it.
"""

from zeroth.contracts.conditions.models import RunConditionResult
from zeroth.contracts.governed import RunState
from zeroth.core.runs.models import (
    Run,
    RunFailureState,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)

__all__ = [
    "Run",
    "RunConditionResult",
    "RunFailureState",
    "RunHistoryEntry",
    "RunState",
    "RunStatus",
    "Thread",
    "ThreadMemoryBinding",
    "ThreadStatus",
]
