"""Run and thread models plus SQLite-backed repositories.

A "run" is a single execution of a graph (workflow). A "thread" groups
multiple runs together so you can track an ongoing conversation or task
across several executions. This package provides the data models for both,
plus repository classes that persist them in SQLite.

The models are imported eagerly; the repositories resolve on first access.
Importing them eagerly meant that reading a run *model* also loaded the
concrete SQL adapter, so any module the adapter imports was loaded while this
package was still initializing -- which makes extracting that adapter into
:mod:`zeroth.integrations.persistence.runs` impossible without a circular
import. ``from zeroth.core.runs import RunRepository`` is unchanged.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from zeroth.core.governed import RunState
from zeroth.core.runs.models import (
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
    from zeroth.core.runs.repository import RunRepository, ThreadRepository

_EXPORTS = {
    "RunRepository": "repository",
    "ThreadRepository": "repository",
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
    """Resolve the concrete repositories from their submodule on first access."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))
