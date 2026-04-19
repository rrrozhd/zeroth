"""Data models for subgraph composition.

Contains the SubgraphNodeData configuration model that is embedded
inside SubgraphNode (defined in ``zeroth.core.graph.models``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SubgraphNodeData(BaseModel):
    """Configuration for a subgraph invocation step.

    Specifies which published graph to invoke as a child workflow,
    how threads are shared, and the maximum nesting depth allowed.
    """

    model_config = ConfigDict(extra="forbid")

    graph_ref: str
    """Name of the published graph to invoke."""

    version: int | None = None
    """Specific deployment version; None means latest active."""

    thread_participation: Literal["inherit", "isolated"] = "inherit"
    """Whether the child run shares the parent's thread or gets its own."""

    max_depth: int = Field(default=3, ge=1, le=10)
    """Maximum recursion depth for nested subgraph invocations."""
