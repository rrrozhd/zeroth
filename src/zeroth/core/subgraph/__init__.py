"""Subgraph composition for governed workflows.

This package provides data models, error hierarchy, and resolution logic
for composing workflows by embedding published graphs as child nodes.

Public API
----------
Models:
    SubgraphNodeData

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
    "SubgraphNodeData",
    "SubgraphResolutionError",
]
