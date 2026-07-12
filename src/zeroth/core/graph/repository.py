"""Save, load, and manage versioned graphs in the database.

The GraphRepository is the main way you interact with stored graphs.
It handles creating, updating, publishing, archiving, cloning, and
diffing graph versions.
"""

from __future__ import annotations

from zeroth.core.graph.diff import GraphDiff, diff_graphs
from zeroth.core.graph.errors import GraphLifecycleError
from zeroth.core.graph.models import Graph, GraphStatus
from zeroth.core.graph.serialization import deserialize_graph, serialize_graph
from zeroth.core.graph.storage import GRAPH_SCHEMA_VERSION
from zeroth.core.graph.validation import GraphValidator
from zeroth.core.graph.versioning import clone_graph_version
from zeroth.core.storage import AsyncDatabase


class GraphRepository:
    """Persistence layer for versioned graph documents."""

    def __init__(
        self,
        database: AsyncDatabase,
        validator: GraphValidator | None = None,
    ):
        self._database: AsyncDatabase = database
        self._validator: GraphValidator | None = validator

    async def save(self, graph: Graph, *, tenant_id: str | None = None) -> Graph:
        """Insert or update a draft graph version.

        WS-B: when ``tenant_id`` is supplied it is stamped onto the graph (so
        the serialized payload and the dedicated ``tenant_id`` column agree)
        before persisting. Omitting it keeps ``graph.tenant_id`` (default
        ``"default"``) for internal/code-authored callers.
        """
        if tenant_id is not None and tenant_id != graph.tenant_id:
            graph = graph.model_copy(update={"tenant_id": tenant_id})
        async with self._database.transaction() as connection:
            existing = await self._fetch_row(connection, graph.graph_id, graph.version)
            if existing is None:
                await self._insert_graph(connection, graph)
            else:
                current = deserialize_graph(existing["payload"])
                if not self._can_update(existing["status"], current, graph):
                    msg = f"graph version {graph.graph_id}@{graph.version} is immutable"
                    raise GraphLifecycleError(msg)
                await self._update_graph(connection, graph)
        return await self.get(graph.graph_id, graph.version)  # type: ignore[return-value]

    async def create(self, graph: Graph, *, tenant_id: str | None = None) -> Graph:
        """Create a new graph (alias for save)."""
        return await self.save(graph, tenant_id=tenant_id)

    async def get(
        self, graph_id: str, version: int | None = None, *, tenant_id: str | None = None
    ) -> Graph | None:
        """Load a graph by ID. Returns the latest version if no version is specified.

        WS-B: when ``tenant_id`` is supplied, a graph owned by a different
        tenant is invisible (returns ``None``) so the API can 404 without
        disclosing existence. ``None`` means no tenant filter (internal path).
        """
        async with self._database.transaction() as connection:
            row = await self._fetch_latest_row(connection, graph_id, version, tenant_id=tenant_id)
        if row is None:
            return None
        return deserialize_graph(row["payload"])

    async def list(self, *, tenant_id: str | None = None) -> list[Graph]:
        """Return the latest version for each graph id (optionally tenant-scoped)."""
        sql = "SELECT payload FROM graph_versions"
        params: tuple[object, ...] = ()
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params = (tenant_id,)
        sql += " ORDER BY graph_id, version"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        latest: dict[str, Graph] = {}
        for row in rows:
            graph = deserialize_graph(row["payload"])
            latest[graph.graph_id] = graph
        return list(latest.values())

    async def list_versions(self, graph_id: str, *, tenant_id: str | None = None) -> list[Graph]:
        """Return all versions of a specific graph, ordered oldest to newest."""
        sql = "SELECT payload FROM graph_versions WHERE graph_id = ?"
        params: tuple[object, ...] = (graph_id,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (graph_id, tenant_id)
        sql += " ORDER BY version"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        return [deserialize_graph(row["payload"]) for row in rows]

    async def publish(self, graph_id: str, version: int | None = None) -> Graph:
        """Move a draft graph to published status so it can be executed.

        Phase 43-02 (D-15): if a ``GraphValidator`` is wired, run
        ``validate_or_raise`` BEFORE the DRAFT -> PUBLISHED state transition.
        A validation failure raises and leaves the graph in DRAFT with its
        persisted state unchanged.
        """
        graph = await self._require(graph_id, version)
        if graph.status is not GraphStatus.DRAFT:
            msg = f"graph version {graph.graph_id}@{graph.version} is not draft"
            raise GraphLifecycleError(msg)
        if self._validator is not None:
            await self._validator.validate_or_raise(graph)
        return await self.save(graph.publish())

    async def archive(self, graph_id: str, version: int | None = None) -> Graph:
        """Archive a graph version so it is no longer active."""
        graph = await self._require(graph_id, version)
        if graph.status is GraphStatus.ARCHIVED:
            return graph
        return await self.save(graph.archive())

    async def clone_published_to_draft(self, graph_id: str, version: int | None = None) -> Graph:
        """Create a new draft version by copying a published graph.

        This is how you edit a published graph: clone it, modify the draft,
        then publish the new version.
        """
        graph = await self._require(graph_id, version)
        if graph.status is not GraphStatus.PUBLISHED:
            msg = f"graph version {graph.graph_id}@{graph.version} is not published"
            raise GraphLifecycleError(msg)
        next_version = await self.get_latest_version(graph_id) + 1
        # WS-B: pin the clone to the source graph's tenant. clone_graph_version
        # preserves tenant_id via model_copy today, but stamping it explicitly
        # keeps the clone from ever landing under the 'default' sentinel (and
        # thus becoming cross-tenant readable) if that helper ever changes.
        return await self.save(
            clone_graph_version(graph, version=next_version, status=GraphStatus.DRAFT),
            tenant_id=graph.tenant_id,
        )

    async def update_status(
        self,
        graph_id: str,
        status: GraphStatus,
        version: int | None = None,
    ) -> Graph:
        """Change a graph's lifecycle status (publish, archive, etc.)."""
        graph = await self._require(graph_id, version)
        if status is GraphStatus.PUBLISHED:
            return await self.publish(graph_id, graph.version)
        if status is GraphStatus.ARCHIVED:
            return await self.archive(graph_id, graph.version)
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

    async def get_latest_version(self, graph_id: str) -> int:
        """Return the highest version number for a graph. Raises KeyError if not found."""
        graph = await self.get(graph_id)
        if graph is None:
            raise KeyError(graph_id)
        return graph.version

    async def diff(self, graph_id: str, left_version: int, right_version: int) -> GraphDiff:
        """Compare two versions of the same graph and return what changed."""
        left = await self._require(graph_id, left_version)
        right = await self._require(graph_id, right_version)
        return diff_graphs(left, right)

    async def _require(self, graph_id: str, version: int | None = None) -> Graph:
        """Load a graph or raise KeyError if it does not exist."""
        graph = await self.get(graph_id, version)
        if graph is None:
            if version is None:
                raise KeyError(graph_id)
            raise KeyError(f"{graph_id}@{version}")
        return graph

    async def _fetch_row(
        self, connection, graph_id: str, version: int, *, tenant_id: str | None = None
    ) -> dict | None:
        """Fetch a single graph row by exact graph_id and version."""
        sql = """
            SELECT status, payload
            FROM graph_versions
            WHERE graph_id = ? AND version = ?
        """
        params: tuple[object, ...] = (graph_id, version)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (graph_id, version, tenant_id)
        return await connection.fetch_one(sql, params)

    async def _fetch_latest_row(
        self, connection, graph_id: str, version: int | None, *, tenant_id: str | None = None
    ) -> dict | None:
        """Fetch a graph row by ID, using the latest version if none is specified."""
        if version is not None:
            return await self._fetch_row(connection, graph_id, version, tenant_id=tenant_id)
        sql = """
            SELECT status, payload
            FROM graph_versions
            WHERE graph_id = ?
        """
        params: tuple[object, ...] = (graph_id,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (graph_id, tenant_id)
        sql += " ORDER BY version DESC LIMIT 1"
        return await connection.fetch_one(sql, params)

    async def _insert_graph(self, connection, graph: Graph) -> None:
        """Insert a new graph version row into the database."""
        await connection.execute(
            """
            INSERT INTO graph_versions (
                graph_id, version, status, schema_version, payload,
                tenant_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                graph.graph_id,
                graph.version,
                graph.status.value,
                GRAPH_SCHEMA_VERSION,
                serialize_graph(graph),
                graph.tenant_id,
                graph.created_at.isoformat(),
                graph.updated_at.isoformat(),
            ),
        )

    async def _update_graph(self, connection, graph: Graph) -> None:
        """Update an existing graph version row in the database."""
        await connection.execute(
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
            return incoming.status is GraphStatus.ARCHIVED and _semantic_graph_dump(
                current
            ) == _semantic_graph_dump(incoming)
        return False


def _semantic_graph_dump(graph: Graph) -> dict[str, object]:
    """Dump a graph to a dict, excluding version/status/timestamps for semantic comparison."""
    return graph.model_dump(
        mode="json",
        exclude={"version", "status", "created_at", "updated_at"},
    )
