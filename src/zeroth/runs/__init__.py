"""Run and thread models plus SQLite-backed repositories.

A "run" is a single execution of a graph (workflow). A "thread" groups
multiple runs together so you can track an ongoing conversation or task
across several executions. This package provides the data models for both,
plus repository classes that persist them in SQLite.
"""

from governai import RunState

from zeroth.runs.models import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)
from zeroth.runs.repository import RunRepository, ThreadRepository

__all__ = [
    "Run",
    "RunConditionResult",
    "RunFailureState",
    "RunHistoryEntry",
    "RunRepository",
    "RunState",
    "RunStatus",
    "Thread",
    "ThreadMemoryBinding",
    "ThreadRepository",
    "ThreadStatus",
]
