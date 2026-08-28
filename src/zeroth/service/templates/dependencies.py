"""Provider-independent dependency inspection for prompt template versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from zeroth.contracts.graph import Graph, GraphStatus
from zeroth.contracts.graph.serialization import deserialize_graph
from zeroth.contracts.templates.errors import TemplateNotFoundError
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


class GraphVersionReader(Protocol):
    """Tenant-scoped graph reads required by template dependency checks."""

    async def list(
        self, *, tenant_id: str | None = None, workspace_id: str | None = None
    ) -> list[Graph]: ...

    async def list_versions(
        self,
        graph_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Graph]: ...


class DeploymentReader(Protocol):
    """Tenant-scoped deployment reads required by template dependency checks."""

    async def list(
        self,
        deployment_ref: str | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class TemplateDependencyConflict:
    """One immutable graph source that still requires a template version."""

    source_kind: str
    source_ref: str
    reference_mode: str


class TemplateDependencyChecker:
    """Inspect immutable graph state before a template version is deleted."""

    def __init__(
        self,
        graph_repository: GraphVersionReader,
        deployment_service: DeploymentReader,
        reference_index: TemplateReferenceIndex | None = None,
    ) -> None:
        self._graph_repository = graph_repository
        self._deployment_service = deployment_service
        self._reference_index = reference_index

    async def find_conflict(
        self,
        *,
        name: str,
        version: int,
        is_latest: bool,
        tenant_id: str,
        workspace_id: str | None,
        transaction: BoundStructuredTable | None = None,
    ) -> TemplateDependencyConflict | None:
        """Return the first scoped published/deployed reference, if one exists."""
        if self._reference_index is not None:
            if (
                tenant_id != self._reference_index.tenant_id
                or workspace_id != self._reference_index.workspace_id
            ):
                raise ValueError("template dependency scope does not match bound index")
            return await self._reference_index.find_conflict(
                name=name,
                version=version,
                is_latest=is_latest,
                transaction=transaction,
            )
        if transaction is not None:
            raise RuntimeError("transactional template dependency index is not configured")
        latest_graphs = await self._graph_repository.list(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        published_graphs: list[Graph] = []
        for latest_graph in latest_graphs:
            versions = await self._graph_repository.list_versions(
                latest_graph.graph_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            published_graphs.extend(
                graph for graph in versions if graph.status is GraphStatus.PUBLISHED
            )
        deployments = await self._deployment_service.list(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

        for graph in published_graphs:
            mode = _matching_reference_mode(
                graph,
                name=name,
                version=version,
                is_latest=is_latest,
            )
            if mode is not None:
                return TemplateDependencyConflict(
                    source_kind="published_graph",
                    source_ref=f"{graph.graph_id}@{graph.version}",
                    reference_mode=mode,
                )
        for deployment in deployments:
            graph = deserialize_graph(deployment.serialized_graph)
            mode = _matching_reference_mode(
                graph,
                name=name,
                version=version,
                is_latest=is_latest,
            )
            if mode is not None:
                return TemplateDependencyConflict(
                    source_kind="deployment",
                    source_ref=f"{deployment.deployment_ref}@{deployment.version}",
                    reference_mode=mode,
                )
        return None


@persistence_surface(
    "service.template_dependency_references",
    probe=named_isolation_probe("_drive_template_dependency_references"),
)
class TemplateReferenceIndex:
    """Durable dependency index updated in graph/deployment write transactions."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        self._database = database
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        if workspace_id is None:
            context = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant_id == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant_id)
            )
        else:
            context = (
                ScopeContext.for_default_compatibility(workspace_id=workspace_id)
                if tenant_id == "default"
                else ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)
            )
        self._references = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.template_dependency_references",
            context,
        )
        self._templates = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.prompt_templates",
            context,
        )
        self._graphs = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.graph_versions",
            context,
        )
        self._deployments = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.deployment_versions",
            context,
        )

    @persistence_operation(
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.ENUMERATE,
        ResourceOperation.DELETE,
    )
    async def sync_graph(
        self,
        transaction: BoundStructuredTable,
        graph: Graph,
    ) -> None:
        """Replace one graph version's index rows inside its persistence transaction."""
        if (graph.tenant_id, graph.workspace_id) != (self.tenant_id, self.workspace_id):
            raise ValueError("graph scope does not match template reference index")
        await self._sync_source(
            transaction,
            source_kind="published_graph",
            source_ref=f"{graph.graph_id}@{graph.version}",
            graph=graph if graph.status is GraphStatus.PUBLISHED else None,
        )

    @persistence_operation(
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.ENUMERATE,
        ResourceOperation.DELETE,
    )
    async def sync_deployment(
        self,
        transaction: BoundStructuredTable,
        *,
        deployment_ref: str,
        version: int,
        serialized_graph: str,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        """Index an immutable deployment snapshot in its create transaction."""
        if (tenant_id, workspace_id) != (self.tenant_id, self.workspace_id):
            raise ValueError("deployment scope does not match template reference index")
        await self._sync_source(
            transaction,
            source_kind="deployment",
            source_ref=f"{deployment_ref}@{version}",
            graph=deserialize_graph(serialized_graph),
        )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.ENUMERATE)
    async def find_conflict(
        self,
        *,
        name: str,
        version: int,
        is_latest: bool,
        transaction: BoundStructuredTable | None = None,
    ) -> TemplateDependencyConflict | None:
        """Read the first matching reference, optionally in a caller's transaction."""
        if transaction is None:
            async with self._references.transaction() as references:
                return await self._find_conflict(
                    references,
                    name=name,
                    version=version,
                    is_latest=is_latest,
                )
        return await self._find_conflict(
            transaction.bind(self._references),
            name=name,
            version=version,
            is_latest=is_latest,
        )

    @persistence_operation(
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.ENUMERATE,
        ResourceOperation.DELETE,
    )
    async def rebuild(self) -> None:
        """Reconcile references created before the index was wired into repositories."""
        async with self._references.transaction(write_lock=True) as references:
            await references.delete(where={"source_kind": "published_graph"})
            await references.delete(where={"source_kind": "deployment"})
            graphs = references.bind(self._graphs)
            rows = await graphs.select(
                where={"status": GraphStatus.PUBLISHED.value},
                columns=("payload",),
            )
            for row in rows:
                graph = deserialize_graph(row["payload"])
                await self._sync_source(
                    references,
                    source_kind="published_graph",
                    source_ref=f"{graph.graph_id}@{graph.version}",
                    graph=graph,
                )
            deployments = references.bind(self._deployments)
            rows = await deployments.select(
                columns=("deployment_ref", "version", "serialized_graph"),
            )
            for row in rows:
                await self._sync_source(
                    references,
                    source_kind="deployment",
                    source_ref=f"{row['deployment_ref']}@{row['version']}",
                    graph=deserialize_graph(row["serialized_graph"]),
                )

    async def _sync_source(
        self,
        transaction: BoundStructuredTable,
        *,
        source_kind: str,
        source_ref: str,
        graph: Graph | None,
    ) -> None:
        references = transaction.bind(self._references)
        await references.delete(
            where={"source_kind": source_kind, "source_ref": source_ref}
        )
        if graph is None:
            return
        templates = transaction.bind(self._templates)
        seen: set[tuple[str, int | None]] = set()
        for node in graph.nodes:
            agent = getattr(node, "agent", None)
            reference = getattr(agent, "template_ref", None)
            if reference is None or (reference.name, reference.version) in seen:
                continue
            seen.add((reference.name, reference.version))
            where: dict[str, object] = {"name": reference.name}
            if reference.version is not None:
                where["version"] = reference.version
            present = await templates.select_one(where=where, columns=("name",))
            if present is None:
                suffix = "" if reference.version is None else f" version {reference.version}"
                raise TemplateNotFoundError(
                    f"Template {reference.name!r}{suffix} not found"
                )
            mode = "latest" if reference.version is None else "explicit"
            key = "latest" if reference.version is None else f"version:{reference.version}"
            await references.insert_if_absent(
                {
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "template_name": reference.name,
                    "template_version": reference.version,
                    "reference_mode": mode,
                    "reference_key": key,
                },
                conflict_columns=(
                    "tenant_id",
                    "workspace_scope",
                    "source_kind",
                    "source_ref",
                    "template_name",
                    "reference_key",
                ),
            )

    async def _find_conflict(
        self,
        references: BoundStructuredTable,
        *,
        name: str,
        version: int,
        is_latest: bool,
    ) -> TemplateDependencyConflict | None:
        rows = await references.select(
            where={"template_name": name},
            columns=(
                "source_kind",
                "source_ref",
                "reference_mode",
                "template_version",
            ),
            order_by=("source_kind", "source_ref"),
        )
        for row in rows:
            if row["reference_mode"] == "explicit" and int(row["template_version"]) != version:
                continue
            if row["reference_mode"] == "latest" and not is_latest:
                continue
            return TemplateDependencyConflict(
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                reference_mode=row["reference_mode"],
            )
        return None


def _matching_reference_mode(
    graph: Graph,
    *,
    name: str,
    version: int,
    is_latest: bool,
) -> str | None:
    for node in graph.nodes:
        agent = getattr(node, "agent", None)
        reference = getattr(agent, "template_ref", None)
        if reference is None or reference.name != name:
            continue
        if reference.version == version:
            return "explicit"
        if reference.version is None and is_latest:
            return "latest"
    return None


__all__ = [
    "TemplateDependencyChecker",
    "TemplateDependencyConflict",
    "TemplateReferenceIndex",
]
