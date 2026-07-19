"""Legacy import path for :mod:`zeroth.contracts.mappings.executor`.

``_get_path`` and ``_set_path`` are republished because existing consumers
import them from this module path even though they are private.
"""

from zeroth.contracts.mappings.executor import (
    MappingExecutor,
    _get_path,
    _set_path,
)

__all__ = [
    "MappingExecutor",
    "_get_path",
    "_set_path",
]
