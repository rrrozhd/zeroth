"""Run and thread domain contracts.

This package is the canonical import location for the run domain: the models
that describe a run and its thread, and the narrow repository protocols the
runtime executes against. Concrete persistence lives outside the runtime and
is injected through these protocols.
"""

from zeroth.runtime.runs.models import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunState,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)
from zeroth.runtime.runs.protocols import (
    CheckpointStore,
    RunReader,
    RunWriter,
    ThreadStore,
)

__all__ = [
    "CheckpointStore",
    "Run",
    "RunConditionResult",
    "RunFailureState",
    "RunHistoryEntry",
    "RunReader",
    "RunState",
    "RunStatus",
    "RunWriter",
    "Thread",
    "ThreadMemoryBinding",
    "ThreadStatus",
    "ThreadStore",
]
