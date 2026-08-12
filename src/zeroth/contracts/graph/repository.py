"""Save, load, and manage versioned graphs in the database.

The GraphRepository is the main way you interact with stored graphs.
It handles creating, updating, publishing, archiving, cloning, and
diffing graph versions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.contracts.graph.diff import GraphDiff, diff_graphs
from zeroth.contracts.graph.errors import GraphLifecycleError
from zeroth.contracts.graph.models import Graph, GraphStatus
from zeroth.contracts.graph.serialization import deserialize_graph, serialize_graph
from zeroth.contracts.graph.storage import GRAPH_SCHEMA_VERSION
from zeroth.contracts.graph.versioning import clone_graph_version
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

if TYPE_CHECKING:
    # Annotation-only: importing the validator eagerly would put the whole
    # validation package on the graph contracts' own import path, and the
    # validators import graph models straight back.
    from zeroth.runtime.graph_validation import GraphValidator


@persistence_surface("service.graph_versions", probe=named_isolation_probe("_drive_graph_versions"))
class GraphRepository:
    """Persistence layer for versioned graph documents."""

    def __init__(
        self,
        database: AsyncDatabase,
        validator: GraphValidator | None = None,
    ):
        self._database: AsyncDatabase = database
        self._validator: GraphValidator | None = validator

    def _graphs(self, tenant_id: str | None, workspace_id: str | None) -> ScopedTable:
        tenant = tenant_id or "default"
        if workspace_id is None:
            context = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant)
            )
        else:
            context = (
                ScopeContext.for_default_compatibility(workspace_id=workspace_id)
                if tenant == "default"
                else ScopeContext(tenant_id=tenant, workspace_id=workspace_id)
            )
        return ScopedTable(
            self._database, SERVICE_SCOPE_REGISTRY, "service.graph_versions", context
        )

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def save(
        self,
        graph: Graph,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Insert or update a draft graph version.

        WS-B: when ``tenant_id`` is supplied it is stamped onto the graph (so
        the serialized payload and the dedicated ``tenant_id`` column agree)
        before persisting. Omitting it keeps ``graph.tenant_id`` (default
        ``"default"``) for internal/code-authored callers.
        """
        scope_requested = tenant_id is not None
        if tenant_id is not None and tenant_id != graph.tenant_id:
            graph = graph.model_copy(update={"tenant_id": tenant_id})
        if tenant_id is not None and workspace_id != graph.workspace_id:
            graph = graph.model_copy(update={"workspace_id": workspace_id})
        async with self._graphs(graph.tenant_id, graph.workspace_id).transaction(
            write_lock=True
        ) as graphs:
            existing = await self._fetch_row(
                graphs,
                graph.graph_id,
                graph.version,
            )
            if existing is None:
                if not await self._insert_graph(graphs, graph):
                    raise KeyError(f"{graph.graph_id}@{graph.version}")
            else:
                current = deserialize_graph(existing["payload"])
                if not self._can_update(existing["status"], current, graph):
                    msg = f"graph version {graph.graph_id}@{graph.version} is immutable"
                    raise GraphLifecycleError(msg)
                await self._update_graph(graphs, graph)
        return await self.get(
            graph.graph_id,
            graph.version,
            tenant_id=graph.tenant_id if scope_requested else None,
            workspace_id=graph.workspace_id,
        )  # type: ignore[return-value]

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.READ)
    async def create(
        self,
        graph: Graph,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Create a new graph (alias for save)."""
        return await self.save(graph, tenant_id=tenant_id, workspace_id=workspace_id)

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self,
        graph_id: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph | None:
        """Load a graph by ID. Returns the latest version if no version is specified.

        WS-B: when ``tenant_id`` is supplied, a graph owned by a different
        tenant is invisible (returns ``None``) so the API can 404 without
        disclosing existence. ``None`` means no tenant filter (internal path).
        """
        async with self._graphs(tenant_id, workspace_id).transaction() as graphs:
            row = await self._fetch_latest_row(
                graphs,
                graph_id,
                version,
            )
        if row is None:
            return None
        return deserialize_graph(row["payload"])

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(
        self, *, tenant_id: str | None = None, workspace_id: str | None = None
    ) -> list[Graph]:
        """Return the latest version for each graph id (optionally tenant-scoped)."""
        async with self._graphs(tenant_id, workspace_id).transaction() as graphs:
            rows = await graphs.select(
                columns=("payload",), order_by=("graph_id", "version")
            )
        latest: dict[str, Graph] = {}
        for row in rows:
            graph = deserialize_graph(row["payload"])
            latest[graph.graph_id] = graph
        return list(latest.values())

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_versions(
        self,
        graph_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Graph]:
        """Return all versions of a specific graph, ordered oldest to newest."""
        async with self._graphs(tenant_id, workspace_id).transaction() as graphs:
            rows = await graphs.select(
                where={"graph_id": graph_id},
                columns=("payload",),
                order_by=("version",),
            )
        return [deserialize_graph(row["payload"]) for row in rows]

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def publish(
        self,
        graph_id: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Move a draft graph to published status so it can be executed.

        Phase 43-02 (D-15): if a ``GraphValidator`` is wired, run
        ``validate_or_raise`` BEFORE the DRAFT -> PUBLISHED state transition.
        A validation failure raises and leaves the graph in DRAFT with its
        persisted state unchanged.
        """
        graph = await self._require(
            graph_id, version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        if graph.status is not GraphStatus.DRAFT:
            msg = f"graph version {graph.graph_id}@{graph.version} is not draft"
            raise GraphLifecycleError(msg)
        if self._validator is not None:
            await self._validator.validate_or_raise(graph)
        return await self.save(
            graph.publish(), tenant_id=graph.tenant_id, workspace_id=graph.workspace_id
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def archive(
        self,
        graph_id: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Archive a graph version so it is no longer active."""
        graph = await self._require(
            graph_id, version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        if graph.status is GraphStatus.ARCHIVED:
            return graph
        return await self.save(
            graph.archive(), tenant_id=graph.tenant_id, workspace_id=graph.workspace_id
        )

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def clone_published_to_draft(
        self,
        graph_id: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Create a new draft version by copying a published graph.

        This is how you edit a published graph: clone it, modify the draft,
        then publish the new version.
        """
        graph = await self._require(
            graph_id, version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        if graph.status is not GraphStatus.PUBLISHED:
            msg = f"graph version {graph.graph_id}@{graph.version} is not published"
            raise GraphLifecycleError(msg)
        next_version = (
            await self.get_latest_version(
                graph_id, tenant_id=graph.tenant_id, workspace_id=graph.workspace_id
            )
            + 1
        )
        # WS-B: pin the clone to the source graph's tenant. clone_graph_version
        # preserves tenant_id via model_copy today, but stamping it explicitly
        # keeps the clone from ever landing under the 'default' sentinel (and
        # thus becoming cross-tenant readable) if that helper ever changes.
        return await self.save(
            clone_graph_version(graph, version=next_version, status=GraphStatus.DRAFT),
            tenant_id=graph.tenant_id,
            workspace_id=graph.workspace_id,
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def update_status(
        self,
        graph_id: str,
        status: GraphStatus,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Change a graph's lifecycle status (publish, archive, etc.)."""
        graph = await self._require(
            graph_id, version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        if status is GraphStatus.PUBLISHED:
            return await self.publish(
                graph_id,
                graph.version,
                tenant_id=graph.tenant_id,
                workspace_id=graph.workspace_id,
            )
        if status is GraphStatus.ARCHIVED:
            return await self.archive(
                graph_id,
                graph.version,
                tenant_id=graph.tenant_id,
                workspace_id=graph.workspace_id,
            )
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

    @persistence_operation(ResourceOperation.READ)
    async def get_latest_version(
        self,
        graph_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Return the highest version number for a graph. Raises KeyError if not found."""
        graph = await self.get(graph_id, tenant_id=tenant_id, workspace_id=workspace_id)
        if graph is None:
            raise KeyError(graph_id)
        return graph.version

    @persistence_operation(ResourceOperation.READ)
    async def diff(
        self,
        graph_id: str,
        left_version: int,
        right_version: int,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> GraphDiff:
        """Compare two versions of the same graph and return what changed."""
        left = await self._require(
            graph_id, left_version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        right = await self._require(
            graph_id, right_version, tenant_id=tenant_id, workspace_id=workspace_id
        )
        return diff_graphs(left, right)

    async def _require(
        self,
        graph_id: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Graph:
        """Load a graph or raise KeyError if it does not exist."""
        graph = await self.get(graph_id, version, tenant_id=tenant_id, workspace_id=workspace_id)
        if graph is None:
            if version is None:
                raise KeyError(graph_id)
            raise KeyError(f"{graph_id}@{version}")
        return graph

    async def _fetch_row(
        self,
        graphs,
        graph_id: str,
        version: int,
    ) -> dict | None:
        """Fetch a single graph row by exact graph_id and version."""
        return await graphs.select_one(
            where={"graph_id": graph_id, "version": version},
            columns=("status", "payload"),
        )

    async def _fetch_latest_row(
        self,
        graphs,
        graph_id: str,
        version: int | None,
    ) -> dict | None:
        """Fetch a graph row by ID, using the latest version if none is specified."""
        if version is not None:
            return await self._fetch_row(
                graphs,
                graph_id,
                version,
            )
        return await graphs.select_one(
            where={"graph_id": graph_id},
            columns=("status", "payload"),
            order_by_desc=("version",),
        )

    async def _insert_graph(self, graphs, graph: Graph) -> bool:
        """Insert a new graph version row into the database."""
        return await graphs.insert_if_absent(
            {
                "graph_id": graph.graph_id,
                "version": graph.version,
                "status": graph.status.value,
                "schema_version": GRAPH_SCHEMA_VERSION,
                "payload": serialize_graph(graph),
                "created_at": graph.created_at.isoformat(),
                "updated_at": graph.updated_at.isoformat(),
            },
            conflict_columns=("graph_id", "version"),
        )

    async def _update_graph(self, graphs, graph: Graph) -> None:
        """Update an existing graph version row in the database."""
        await graphs.update(
            {
                "status": graph.status.value,
                "schema_version": GRAPH_SCHEMA_VERSION,
                "payload": serialize_graph(graph),
                "updated_at": graph.updated_at.isoformat(),
            },
            where={"graph_id": graph.graph_id, "version": graph.version},
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
