"""Hostile builtin subtypes the tool-normalization gates have to keep out.

Every prior audit on this epic converged on the same attack class: a subclass of
a builtin that ``isinstance`` admits and that runs attacker code from a dunder
the consuming code did not know it was calling. These are those subclasses, one
per hook a normalizer could reach.

:class:`HostileStr` is imported from the ZER-4 helpers rather than redefined --
one definition of the string attack, used by both suites.

These need neither ``langgraph`` nor ``langchain``, so nothing here carries the
``langgraph_conformance`` marker: ``addopts`` deselects it, and a marked test
would never run.
"""

from __future__ import annotations

from typing import Any

from tests.integrations.langgraph.genai._causal import CONTENT_SENTINEL, HostileStr

__all__ = [
    "CONTENT_SENTINEL",
    "HostileDict",
    "HostileKey",
    "HostileList",
    "HostileStr",
    "HostileTuple",
]


class HostileDict(dict):
    """A ``dict`` that answers one thing to ``items()`` and another to a lookup.

    The point of iterating ``items()`` instead of keying back in: a normalizer
    that validates what iteration yielded and then *re-reads* the value by key
    validates one object and stores a different one.
    """

    def __getitem__(self, key: Any) -> Any:
        return CONTENT_SENTINEL

    def get(self, key: Any, default: Any = None) -> Any:
        """Return the sentinel for any key, as ``__getitem__`` does."""
        return CONTENT_SENTINEL


class HostileKey(str):
    """A ``str`` key whose ``__hash__`` and ``__eq__`` run on every lookup.

    Stored in a mapping, it turns ``mapping[key]`` -- anyone's, later -- into a
    call into this class. The exact-type gate on keys is what stops it from ever
    being stored.
    """

    hashed = 0
    compared = 0

    def __hash__(self) -> int:
        type(self).hashed += 1
        return hash(CONTENT_SENTINEL)

    def __eq__(self, other: object) -> bool:
        type(self).compared += 1
        return True


class HostileTuple(tuple):
    """A ``tuple`` whose ``__iter__`` yields values the tuple does not contain.

    A copy built by iterating gets the injected values, not the ones a reviewer
    reading ``tuple(x)`` would expect. Only refusing the subtype outright avoids
    the question.
    """

    def __iter__(self) -> Any:
        yield CONTENT_SENTINEL


class HostileList(list):
    """A ``list`` that injects a value through ``__iter__``, as :class:`HostileTuple` does."""

    def __iter__(self) -> Any:
        yield CONTENT_SENTINEL
