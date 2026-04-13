"""Subgraph composition for governed workflows.

This package provides data models, error hierarchy, resolution logic,
and the execution engine for composing workflows by embedding published
graphs as child nodes.

Public API
----------
Models:
    SubgraphNodeData

Executor:
    SubgraphExecutor

Errors:
    SubgraphError, SubgraphDepthLimitError, SubgraphResolutionError,
    SubgraphExecutionError, SubgraphCycleError

Resolver (import from ``zeroth.core.subgraph.resolver`` to avoid circular imports):
    SubgraphResolver, namespace_subgraph, merge_governance
"""

from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.core.subgraph.models import SubgraphNodeData

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
        from zeroth.core.subgraph.executor import SubgraphExecutor

        return SubgraphExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
