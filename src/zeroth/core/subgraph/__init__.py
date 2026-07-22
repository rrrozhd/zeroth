"""Legacy import path for the runtime subgraphs package.

Subgraph composition lives in :mod:`zeroth.runtime.subgraphs`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.runtime.subgraphs import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphError,
    SubgraphExecutionError,
    SubgraphNodeData,
    SubgraphResolutionError,
)

__all__ = [
    "SubgraphCycleError",
    "SubgraphDepthLimitError",
    "SubgraphError",
    "SubgraphExecutionError",
    "SubgraphExecutor",
    "SubgraphNodeData",
    "SubgraphResolutionError",
]


def __getattr__(name: str) -> object:
    """Lazy import for SubgraphExecutor to avoid circular import with graph.models."""
    if name == "SubgraphExecutor":
        from zeroth.runtime.subgraphs.executor import SubgraphExecutor

        return SubgraphExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
