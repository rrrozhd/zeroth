"""Importable reducer callables used as fixtures for reducer_ref tests.

These exist as real module-level functions so tests can reference them via
dotted paths like ``tests._fixtures.reducers.sum_ints`` exercising the
``resolve_reducer_ref`` import path.
"""

from __future__ import annotations

from typing import Any


def sum_ints(acc: int, nxt: int) -> int:
    """Add two integer branch outputs."""
    return acc + nxt


def sum_scores(acc: dict[str, Any], nxt: dict[str, Any]) -> dict[str, Any]:
    """Sum the ``total`` key of two dict branch outputs."""
    return {"total": acc.get("total", 0) + nxt.get("total", 0)}


NOT_CALLABLE = 42
"""A module-level non-callable attribute for negative tests."""
