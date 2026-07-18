"""Compatibility re-export of the public graph validator.

The validator now lives in :mod:`zeroth.runtime.graph_validation`: it composes
contract-owned validators with execution checks that need the runtime and
governance layers, which the contracts layer may not import.

Resolution is lazy on purpose. This module sits in the contracts layer, and an
eager runtime import here would put the whole runtime on the import path of
anything that reaches ``zeroth.core.graph`` -- the inversion that made the
canonical packages uncold-importable before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zeroth.runtime.graph_validation import GraphValidator

__all__ = ["GraphValidator"]


def __getattr__(name: str) -> Any:
    if name == "GraphValidator":
        from zeroth.runtime.graph_validation import GraphValidator

        return GraphValidator
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
