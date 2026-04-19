"""Helpers for creating new versions of a graph.

When you want to edit a published graph, you first clone it into a new
draft version.  The functions here handle that cloning logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.core.graph.models import Graph, GraphStatus


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def graph_version_ref(graph_id: str, version: int) -> str:
    """Build a version reference string like 'my-graph@3'."""
    return f"{graph_id}@{version}"


def clone_graph_version(graph: Graph, *, version: int, status: GraphStatus) -> Graph:
    """Clone a graph into a new version while retargeting node graph refs."""
    cloned = graph.model_copy(deep=True)
    cloned.version = version
    cloned.status = status
    cloned.created_at = _utc_now()
    cloned.updated_at = cloned.created_at

    version_ref = graph_version_ref(cloned.graph_id, version)
    for node in cloned.nodes:
        node.graph_version_ref = version_ref
    return cloned
