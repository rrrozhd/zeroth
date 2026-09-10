"""Who charges a physical call when a framework hook and a provider adapter both see it.

A framework callback (LangChain, CrewAI, ...) opens a scope around the model
call it observes. A provider adapter (``instrument_openai`` and friends) that
charges the underlying HTTP call marks the current scope. When the framework
callback ends, a marked scope means the physical call is already charged, so
the callback records nothing for it. Scopes are context-local, so parallel
branches in threads or tasks cannot see each other's marks.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class Scope:
    step: str | None = None
    charged: bool = False


_scope: ContextVar[Scope | None] = ContextVar("zeroth_physical_scope", default=None)


def open_scope(step: str | None = None) -> Scope:
    """Start observing a model call from a framework hook; inspect the scope when it ends.

    ``step`` is the framework's name for the call; a provider adapter charging
    inside this scope records under that name instead of its own operation.
    """
    scope = Scope(step=step)
    _scope.set(scope)
    return scope


def close_scope(scope: Scope) -> None:
    """End the framework's observation so later calls do not inherit its name."""
    if _scope.get() is scope:
        _scope.set(None)


def current_scope() -> Scope | None:
    return _scope.get()


def mark_charged() -> None:
    """A provider adapter charged the physical call inside the current scope, if any."""
    scope = _scope.get()
    if scope is not None:
        scope.charged = True
