"""Security release-tooling contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .matrix import Matrix, MatrixError

__all__ = ["Matrix", "MatrixError", "load_matrix"]


def __getattr__(name: str):
    """Load matrix exports lazily so ``python -m`` does not pre-import its target."""
    if name in __all__:
        from . import matrix

        return getattr(matrix, name)
    raise AttributeError(name)
