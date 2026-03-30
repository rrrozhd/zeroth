"""Save, load, and manage versioned graphs in the database.

The GraphRepository is the main way you interact with stored graphs.
It handles creating, updating, publishing, archiving, cloning, and
diffing graph versions.
"""

from __future__ import annotations

from zeroth.graph.diff import GraphDiff, diff_graphs
from zeroth.graph.errors import GraphLifecycleError
from zeroth.graph.models import Graph, GraphStatus
from zeroth.graph.serialization import deserialize_graph, serialize_graph
from zeroth.graph.storage import GRAPH_MIGRATIONS, GRAPH_SCHEMA_SCOPE, GRAPH_SCHEMA_VERSION
from zeroth.graph.versioning import clone_graph_version
from zeroth.storage import SQLiteDatabase


class GraphRepository:
    """Persistence layer for versioned graph documents."""

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(GRAPH_SCHEMA_SCOPE, GRAPH_MIGRATIONS)

    def save(self, graph: Graph) -> Graph:
        """Insert or update a draft graph version."""
        with self._database.transaction() as connection:
            existing = self._fetch_row(connection, graph.graph_id, graph.version)
            if existing is None:
                self._insert_graph(connection, graph)
            else:
                current = deserialize_graph(existing["payload"])
                if not self._can_update(existing["status"], current, graph):
                    msg = f"graph version {graph.graph_id}@{graph.version} is immutable"
                    raise GraphLifecycleError(msg)
                self._update_graph(connection, graph)
        return self.get(graph.graph_id, graph.version)  # type: ignore[return-value]

    def create(self, graph: Graph) -> Graph:
        """Create a new graph (alias for save)."""
        return self.save(graph)

    def get(self, graph_id: str, version: int | None = None) -> Graph | None:
        """Load a graph by ID. Returns the latest version if no version is specified."""
        with self._database.transaction() as connection:
            row = self._fetch_latest_row(connection, graph_id, version)
        if row is None:
            return None
        return deserialize_graph(row["payload"])

    def list(self) -> list[Graph]:
        """Return the latest version for each graph id."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM graph_versions
                ORDER BY graph_id, version
                """
            ).fetchall()
        latest: dict[str, Graph] = {}
        for row in rows:
            graph = deserialize_graph(row["payload"])
            latest[graph.graph_id] = graph
        return list(latest.values())

    def list_versions(self, graph_id: str) -> list[Graph]:
        """Return all versions of a specific graph, ordered oldest to newest."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM graph_versions
                WHERE graph_id = ?
                ORDER BY version
                """,
                (graph_id,),
            ).fetchall()
        return [deserialize_graph(row["payload"]) for row in rows]

    def publish(self, graph_id: str, version: int | None = None) -> Graph:
        """Move a draft graph to published status so it can be executed."""
        graph = self._require(graph_id, version)
        if graph.status is not GraphStatus.DRAFT:
            msg = f"graph version {graph.graph_id}@{graph.version} is not draft"
            raise GraphLifecycleError(msg)
        return self.save(graph.publish())

    def archive(self, graph_id: str, version: int | None = None) -> Graph:
        """Archive a graph version so it is no longer active."""
        graph = self._require(graph_id, version)
        if graph.status is GraphStatus.ARCHIVED:
            return graph
        return self.save(graph.archive())

    def clone_published_to_draft(self, graph_id: str, version: int | None = None) -> Graph:
        """Create a new draft version by copying a published graph.

        This is how you edit a published graph: clone it, modify the draft,
        then publish the new version.
        """
        graph = self._require(graph_id, version)
        if graph.status is not GraphStatus.PUBLISHED:
            msg = f"graph version {graph.graph_id}@{graph.version} is not published"
            raise GraphLifecycleError(msg)
        next_version = self.get_latest_version(graph_id) + 1
        return self.save(
            clone_graph_version(graph, version=next_version, status=GraphStatus.DRAFT)
        )

    def update_status(
        self,
        graph_id: str,
        status: GraphStatus,
        version: int | None = None,
    ) -> Graph:
        """Change a graph's lifecycle status (publish, archive, etc.)."""
        graph = self._require(graph_id, version)
        if status is GraphStatus.PUBLISHED:
            return self.publish(graph_id, graph.version)
        if status is GraphStatus.ARCHIVED:
            return self.archive(graph_id, graph.version)
        if status is GraphStatus.DRAFT:
            if graph.status is GraphStatus.DRAFT:
                return graph
            msg = (
                f"graph version {graph.graph_id}@{graph.version} cannot revert to draft; "
                "clone the published version instead"
            )
            raise GraphLifecycleError(msg)
        msg = f"unsupported graph status: {status}"
        raise GraphLifecycleError(msg)

    def get_latest_version(self, graph_id: str) -> int:
        """Return the highest version number for a graph. Raises KeyError if not found."""
        graph = self.get(graph_id)
        if graph is None:
            raise KeyError(graph_id)
        return graph.version

    def diff(self, graph_id: str, left_version: int, right_version: int) -> GraphDiff:
        """Compare two versions of the same graph and return what changed."""
        left = self._require(graph_id, left_version)
        right = self._require(graph_id, right_version)
        return diff_graphs(left, right)

    def _require(self, graph_id: str, version: int | None = None) -> Graph:
        """Load a graph or raise KeyError if it does not exist."""
        graph = self.get(graph_id, version)
        if graph is None:
            if version is None:
                raise KeyError(graph_id)
            raise KeyError(f"{graph_id}@{version}")
        return graph

    def _fetch_row(self, connection, graph_id: str, version: int) -> object | None:
        """Fetch a single graph row by exact graph_id and version."""
        return connection.execute(
            """
            SELECT status, payload
            FROM graph_versions
            WHERE graph_id = ? AND version = ?
            """,
            (graph_id, version),
        ).fetchone()

    def _fetch_latest_row(self, connection, graph_id: str, version: int | None) -> object | None:
        """Fetch a graph row by ID, using the latest version if none is specified."""
        if version is not None:
            return self._fetch_row(connection, graph_id, version)
        return connection.execute(
            """
            SELECT status, payload
            FROM graph_versions
            WHERE graph_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (graph_id,),
        ).fetchone()

    def _insert_graph(self, connection, graph: Graph) -> None:
        """Insert a new graph version row into the database."""
        connection.execute(
            """
            INSERT INTO graph_versions (
                graph_id, version, status, schema_version, payload, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                graph.graph_id,
                graph.version,
                graph.status.value,
                GRAPH_SCHEMA_VERSION,
                serialize_graph(graph),
                graph.created_at.isoformat(),
                graph.updated_at.isoformat(),
            ),
        )

    def _update_graph(self, connection, graph: Graph) -> None:
        """Update an existing graph version row in the database."""
        connection.execute(
            """
            UPDATE graph_versions
            SET status = ?, schema_version = ?, payload = ?, updated_at = ?
            WHERE graph_id = ? AND version = ?
            """,
            (
                graph.status.value,
                GRAPH_SCHEMA_VERSION,
                serialize_graph(graph),
                graph.updated_at.isoformat(),
                graph.graph_id,
                graph.version,
            ),
        )

    def _can_update(self, stored_status: str, current: Graph, incoming: Graph) -> bool:
        """Check if the stored graph version is allowed to be overwritten."""
        if stored_status == GraphStatus.DRAFT.value:
            return True
        if stored_status == GraphStatus.PUBLISHED.value:
            return (
                incoming.status is GraphStatus.ARCHIVED
                and _semantic_graph_dump(current) == _semantic_graph_dump(incoming)
            )
        return False


def _semantic_graph_dump(graph: Graph) -> dict[str, object]:
    """Dump a graph to a dict, excluding version/status/timestamps for semantic comparison."""
    return graph.model_dump(
        mode="json",
        exclude={"version", "status", "created_at", "updated_at"},
    )
