"""Tenant/workspace-scoped durable prompt-template registry."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from zeroth.contracts.templates.errors import (
    TemplateNotFoundError,
    TemplateVersionExistsError,
)
from zeroth.contracts.templates.models import PromptTemplate
from zeroth.contracts.templates.registry import TemplateRegistry
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


@persistence_surface(
    "service.prompt_templates", probe=named_isolation_probe("_drive_prompt_templates")
)
class DatabaseTemplateRegistry:
    """Persist immutable template versions in the service database."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        self._database = database
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._workspace_scope = "null" if workspace_id is None else f"value:{workspace_id}"
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
        self._templates = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.prompt_templates",
            context,
        )

    @staticmethod
    def _validated(
        name: str,
        version: int,
        template_str: str,
        *,
        variables: list[str] | None,
        metadata: dict[str, object] | None,
    ) -> PromptTemplate:
        registry = TemplateRegistry()
        return registry.register(
            name,
            version,
            template_str,
            variables=variables,
            metadata=metadata,
        )

    def _scope_params(self) -> tuple[str, str]:
        return self._tenant_id, self._workspace_scope

    @staticmethod
    def _from_row(row: dict[str, Any]) -> PromptTemplate:
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return PromptTemplate(
            name=row["name"],
            version=int(row["version"]),
            template_str=row["template_str"],
            variables=json.loads(row["variables_json"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=created_at,
        )

    @persistence_operation(ResourceOperation.CREATE)
    async def register(
        self,
        name: str,
        version: int,
        template_str: str,
        *,
        variables: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PromptTemplate:
        async with self.mutation_transaction() as transaction:
            return await self.register_in_transaction(
                transaction,
                name,
                version,
                template_str,
                variables=variables,
                metadata=metadata,
            )

    @asynccontextmanager
    async def mutation_transaction(self) -> AsyncIterator[BoundStructuredTable]:
        """Open the shared write transaction used by mutation, index, and audit."""
        async with self._templates.transaction(write_lock=True) as templates:
            yield templates

    async def register_in_transaction(
        self,
        transaction: BoundStructuredTable,
        name: str,
        version: int,
        template_str: str,
        *,
        variables: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PromptTemplate:
        """Insert a validated immutable version in a caller-owned transaction."""
        template = self._validated(
            name,
            version,
            template_str,
            variables=variables,
            metadata=metadata,
        )
        templates = transaction.bind(self._templates)
        existing = await templates.select_one(
            where={"name": name, "version": version},
            columns=("name",),
        )
        if existing is not None:
            raise TemplateVersionExistsError(
                f"Template {name!r} version {version} already exists"
            )
        try:
            inserted = await templates.insert_if_absent(
                {
                    "name": name,
                    "version": version,
                    "template_str": template.template_str,
                    "variables_json": json.dumps(
                        template.variables, separators=(",", ":"), sort_keys=True
                    ),
                    "metadata_json": json.dumps(
                        template.metadata, separators=(",", ":"), sort_keys=True
                    ),
                    "created_at": template.created_at.isoformat(),
                },
                conflict_columns=("tenant_id", "workspace_scope", "name", "version"),
            )
            if not inserted:
                raise TemplateVersionExistsError(
                    f"Template {name!r} version {version} already exists"
                )
        except TemplateVersionExistsError:
            raise
        except Exception:
            duplicate = await templates.select_one(
                where={"name": name, "version": version},
                columns=("name",),
            )
            if duplicate is not None:
                raise TemplateVersionExistsError(
                    f"Template {name!r} version {version} already exists"
                ) from None
            raise
        return template

    @persistence_operation(ResourceOperation.READ)
    async def get(self, name: str, version: int | None = None) -> PromptTemplate:
        async with self._templates.transaction() as templates:
            return await self.get_in_transaction(templates, name, version)

    async def get_in_transaction(
        self,
        transaction: BoundStructuredTable,
        name: str,
        version: int | None = None,
    ) -> PromptTemplate:
        """Read one version without leaving the caller's transaction snapshot."""
        version_clause = "" if version is None else "AND version = ?"
        order_clause = "ORDER BY version DESC" if version is None else ""
        params: tuple[Any, ...] = (*self._scope_params(), name)
        if version is not None:
            params = (*params, version)
        del version_clause, order_clause, params
        where: dict[str, object] = {"name": name}
        if version is not None:
            where["version"] = version
        row = await transaction.bind(self._templates).select_one(
            where=where,
            columns=(
                "name",
                "version",
                "template_str",
                "variables_json",
                "metadata_json",
                "created_at",
            ),
            order_by_desc=("version",) if version is None else (),
        )
        if row is None:
            suffix = "" if version is None else f" version {version}"
            raise TemplateNotFoundError(f"Template {name!r}{suffix} not found")
        return self._from_row(row)

    async def get_latest(self, name: str) -> PromptTemplate:
        return await self.get(name)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(self) -> list[PromptTemplate]:
        async with self._templates.transaction() as templates:
            rows = await templates.select(
                columns=(
                    "name",
                    "version",
                    "template_str",
                    "variables_json",
                    "metadata_json",
                    "created_at",
                ),
                order_by=("name", "version"),
            )
        return [self._from_row(row) for row in rows]

    @persistence_operation(ResourceOperation.DELETE)
    async def delete(self, name: str, version: int) -> None:
        async with self.mutation_transaction() as transaction:
            await self.delete_in_transaction(transaction, name, version)

    async def delete_in_transaction(
        self,
        transaction: BoundStructuredTable,
        name: str,
        version: int,
    ) -> None:
        """Delete one existing version in a caller-owned transaction."""
        templates = transaction.bind(self._templates)
        existing = await templates.select_one(
            where={"name": name, "version": version},
            columns=("name",),
        )
        if existing is None:
            raise TemplateNotFoundError(f"Template {name!r} version {version} not found")
        await templates.delete(where={"name": name, "version": version})


__all__ = ["DatabaseTemplateRegistry"]
