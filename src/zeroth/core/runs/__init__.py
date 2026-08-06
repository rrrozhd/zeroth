"""Run and thread models plus SQLite-backed repositories.

A "run" is a single execution of a graph (workflow). A "thread" groups
multiple runs together so you can track an ongoing conversation or task
across several executions. This package provides the data models for both,
plus repository classes that persist them in SQLite.

The concrete repositories now live in :mod:`zeroth.integrations.persistence.runs`
and are resolved from there on first access, so ``from zeroth.core.runs import
RunRepository`` is unchanged.

They stay lazy for the reason that made the extraction possible in the first
place. Importing them eagerly meant that reading a run *model* also loaded the
concrete SQL adapter, so every module the adapter imports was loaded while this
package was still initializing -- which closed a circular import the moment the
adapter lived outside ``zeroth.core``. Making this resolution eager again would
reintroduce that cycle.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from zeroth.contracts.governed import RunState
from zeroth.runtime.runs.models import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)

if TYPE_CHECKING:
    from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository

_EXPORTS = {
    "RunRepository": "zeroth.integrations.persistence.runs.run_repository",
    "ThreadRepository": "zeroth.integrations.persistence.runs.thread_repository",
}

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


def __getattr__(name: str) -> object:
    """Resolve the concrete repositories from the persistence package on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))
