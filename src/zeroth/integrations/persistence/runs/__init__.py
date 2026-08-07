"""Concrete SQL persistence for the run and thread domain.

This package owns the database side of runs, threads, and checkpoints. The
domain models and the narrow repository protocols runtime code executes
against live in :mod:`zeroth.runtime.runs`; nothing here is imported by the
runtime, which receives these adapters through injection.

Submodules are imported on access rather than eagerly. ``zeroth.runtime.runs``
resolves :class:`RunRepository` and :class:`ThreadRepository` lazily so that
reading a run *model* does not drag in the SQL adapter, and keeping this
package lazy in the same way preserves that property for consumers who import
the canonical path directly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.integrations.persistence.runs.run_repository import RunRepository
    from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository

_EXPORTS = {
    "RunRepository": "run_repository",
    "ThreadRepository": "thread_repository",
}

__all__ = [
    "RunRepository",
    "ThreadRepository",
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
