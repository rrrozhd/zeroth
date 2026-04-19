"""Convert graph objects to and from JSON strings.

Used by the repository layer to store graphs in the database and
read them back out again.
"""

from __future__ import annotations

from zeroth.core.graph.models import Graph
from zeroth.core.storage.json import load_model, to_json_value


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
