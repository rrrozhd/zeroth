"""Convert graph objects to and from JSON strings.

Used by the repository layer to store graphs in the database and
read them back out again.
"""

from __future__ import annotations

from typing import Protocol

from zeroth.contracts.graph.models import Graph
from zeroth.platform.storage.json import load_model, to_json_value


def serialize_graph(graph: Graph) -> str:
    """Turn a Graph object into a JSON string for storage."""
    return to_json_value(graph)


def deserialize_graph(raw: str | bytes) -> Graph:
    """Turn a JSON string (from the database) back into a Graph object.

    Raises ValueError if the payload is empty or invalid.
    """
    graph = load_model(raw, Graph)
    if graph is None:
        msg = "graph payload cannot be empty"
        raise ValueError(msg)
    return graph


class DeployedGraphSnapshot(Protocol):
    """Minimal immutable deployment shape needed to hydrate a graph."""

    serialized_graph: str
    engine_mode: object


def hydrate_deployed_graph(deployment: DeployedGraphSnapshot) -> Graph:
    """Deserialize a deployment and apply its immutable engine-mode pin."""
    graph = deserialize_graph(deployment.serialized_graph)
    engine_mode = getattr(deployment.engine_mode, "value", deployment.engine_mode)
    if engine_mode not in {"legacy", "token"}:
        raise ValueError(f"unknown deployment engine mode: {engine_mode!r}")
    settings = graph.execution_settings.model_copy(
        update={"sequential_join_enabled": engine_mode == "token"}
    )
    return graph.model_copy(update={"execution_settings": settings})
