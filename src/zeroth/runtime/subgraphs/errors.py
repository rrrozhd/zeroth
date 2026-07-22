"""Error hierarchy for subgraph composition.

These exceptions cover the different failure modes that can occur during
subgraph resolution and execution: depth limit violations, resolution
failures, execution errors, and circular reference detection.
"""

from __future__ import annotations


class SubgraphError(RuntimeError):
    """Base error for all subgraph-related failures.

    All subgraph exceptions inherit from this class so callers can
    catch any subgraph problem with a single ``except SubgraphError``.
    """


class SubgraphDepthLimitError(SubgraphError):
    """Subgraph nesting depth exceeds the configured maximum.

    Raised when a subgraph reference would exceed the ``max_depth``
    limit configured on the SubgraphNodeData.
    """


class SubgraphResolutionError(SubgraphError):
    """A subgraph graph_ref could not be resolved to a deployment.

    Raised when ``SubgraphResolver.resolve()`` cannot find a matching
    deployment for the given graph reference and optional version.
    """


class SubgraphExecutionError(SubgraphError):
    """A child run spawned for a subgraph failed during execution.

    Carries context about which subgraph and run encountered the error.
    """


class SubgraphCycleError(SubgraphError):
    """Circular subgraph reference detected.

    Raised when resolving subgraph references reveals a cycle
    (graph A references graph B which references graph A).
    """
