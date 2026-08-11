"""Structured async SQL gateways that make resource scope unavoidable."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

from zeroth.platform.storage.database import AsyncDatabase
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
    TenantWideScopeContext,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OWNERSHIP_COLUMNS = frozenset({"tenant_id", "workspace_id"})

_SERVICE_TABLES = (
    "approvals",
    "audit_chain_heads",
    "contract_versions",
    "decision_records",
    "deployment_versions",
    "enforcement_heartbeats",
    "graph_versions",
    "langgraph_decisions",
    "langgraph_inventories",
    "langgraph_run_attestations",
    "legal_holds",
    "memory_connector_configs",
    "node_audits",
    "quota_counters",
    "rate_limit_buckets",
    "retention_audit_log",
    "retention_cleanup_operations",
    "retention_cleanup_state",
    "retention_coordination",
    "retention_policies",
    "run_attestations",
    "run_checkpoints",
    "runs",
    "side_effect_operations",
    "threads",
    "token_engine_snapshots",
    "tool_inventory_registrations",
    "webhook_dead_letters",
    "webhook_deliveries",
    "webhook_subscriptions",
)
_SERVICE_WORKSPACE_TABLES = frozenset(
    {
        "approvals",
        "deployment_versions",
        "graph_versions",
        "node_audits",
        "run_checkpoints",
        "runs",
        "threads",
    }
)

SERVICE_SCOPE_DEFINITIONS = tuple(
    ResourceScopeDefinition(
        resource_name=f"service.{table_name}",
        table_name=table_name,
        workspace_scoped=table_name in _SERVICE_WORKSPACE_TABLES,
        operations=frozenset(ResourceOperation),
    )
    for table_name in _SERVICE_TABLES
) + (
    ResourceScopeDefinition(
        resource_name="service.alembic_version",
        table_name="alembic_version",
        scope=ResourceScope.GLOBAL,
        operations=frozenset({ResourceOperation.READ}),
    ),
    ResourceScopeDefinition(
        resource_name="service.schema_versions",
        table_name="schema_versions",
        scope=ResourceScope.GLOBAL,
        operations=frozenset({ResourceOperation.READ}),
    ),
)
"""Scope definitions for every physical table in the service migration head."""

ECON_MIGRATION_SCOPE_DEFINITIONS = (
    ResourceScopeDefinition(
        resource_name="econ.alembic_version",
        table_name="alembic_version",
        scope=ResourceScope.GLOBAL,
        operations=frozenset({ResourceOperation.READ}),
    ),
    ResourceScopeDefinition(
        resource_name="econ.auth_scope_migration_provenance",
        table_name="_zeroth_20260811_04_auth_scope",
        scope=ResourceScope.GLOBAL,
        operations=frozenset({ResourceOperation.READ}),
    ),
)
"""Unmapped bookkeeping tables in the econ migration head."""

SERVICE_SCOPE_REGISTRY = ResourceScopeRegistry(SERVICE_SCOPE_DEFINITIONS)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return value


def _columns(values: tuple[str, ...], *, qualifier: str | None = None) -> str:
    if values == ("*",):
        return f"{qualifier}.*" if qualifier else "*"
    if not values:
        raise ValueError("columns must be non-empty")
    rendered = [_identifier(value) for value in values]
    if qualifier is not None:
        rendered = [f"{qualifier}.{value}" for value in rendered]
    return ", ".join(rendered)


@dataclass(frozen=True, slots=True)
class ScopedJoin:
    """A tenant-safe inner join to another scoped table."""

    table: ScopedTable
    local_column: str
    foreign_column: str

    def __post_init__(self) -> None:
        if type(self.table) is not ScopedTable:
            raise TypeError("join table must be a ScopedTable")
        _identifier(self.local_column)
        _identifier(self.foreign_column)


class _StructuredTable:
    __slots__ = ("__database", "_definition", "_registry")

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        definition: ResourceScopeDefinition,
    ) -> None:
        if not isinstance(database, AsyncDatabase):
            raise TypeError("database must implement AsyncDatabase")
        if type(registry) is not ResourceScopeRegistry:
            raise TypeError("registry must be a ResourceScopeRegistry")
        self.__database = database
        self._registry = registry
        self._definition = definition

    def _validate_operation(self, operation: ResourceOperation) -> None:
        raise NotImplementedError

    def _scope_items(self) -> tuple[tuple[str, str], ...]:
        return ()

    def _validate_values(self, values: dict[str, Any], *, create: bool) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be a non-empty dict")
        rendered = {_identifier(key): value for key, value in values.items()}
        scope_items = dict(self._scope_items())
        if create:
            for column, expected in scope_items.items():
                if column in rendered and rendered[column] != expected:
                    raise ValueError(f"{column} does not match bound scope")
                rendered[column] = expected
        elif _OWNERSHIP_COLUMNS.intersection(rendered):
            raise ValueError("ownership columns cannot be updated")
        return rendered

    def _where(
        self,
        where: dict[str, Any] | None,
        *,
        qualifier: str | None = None,
        include_scope: bool = True,
    ) -> tuple[list[str], list[Any]]:
        predicates: list[str] = []
        params: list[Any] = []
        for column, value in (where or {}).items():
            identifier = _identifier(column)
            rendered = f"{qualifier}.{identifier}" if qualifier else identifier
            predicates.append(f"{rendered} = ?")
            params.append(value)
        if include_scope:
            for column, value in self._scope_items():
                rendered = f"{qualifier}.{column}" if qualifier else column
                predicates.append(f"{rendered} = ?")
                params.append(value)
        return predicates, params

    async def select(
        self,
        *,
        where: dict[str, Any] | None = None,
        columns: tuple[str, ...] = ("*",),
        joins: tuple[ScopedJoin, ...] = (),
    ) -> list[dict[str, Any]]:
        """Return scoped rows selected through structured equality predicates."""
        self._validate_operation(ResourceOperation.ENUMERATE)
        table_name = self._definition.table_name
        selected = _columns(columns, qualifier=table_name if joins else None)
        sql = f"SELECT {selected} FROM {table_name}"
        predicates, params = self._where(where, qualifier=table_name if joins else None)
        for index, join in enumerate(joins, start=1):
            if type(self) is not ScopedTable:
                raise ValueError("global tables cannot join through a scoped gateway")
            assert isinstance(self, ScopedTable)
            self._validate_join(join.table)
            alias = f"j{index}"
            joined_name = join.table._definition.table_name
            sql += (
                f" JOIN {joined_name} AS {alias} ON "
                f"{table_name}.{join.local_column} = {alias}.{join.foreign_column}"
            )
            join_predicates, join_params = join.table._where(None, qualifier=alias)
            predicates.extend(join_predicates)
            params.extend(join_params)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        async with self.__database.transaction() as connection:
            return await connection.fetch_all(sql, tuple(params))

    async def select_one(
        self,
        *,
        where: dict[str, Any],
        columns: tuple[str, ...] = ("*",),
    ) -> dict[str, Any] | None:
        """Return one scoped row or ``None``."""
        self._validate_operation(ResourceOperation.READ)
        predicates, params = self._where(where)
        sql = (
            f"SELECT {_columns(columns)} FROM {self._definition.table_name} WHERE "
            + " AND ".join(predicates)
            + " LIMIT 1"
        )
        async with self.__database.transaction() as connection:
            return await connection.fetch_one(sql, tuple(params))

    async def insert(self, values: dict[str, Any]) -> None:
        """Insert a row after filling and validating its ownership columns."""
        self._validate_operation(ResourceOperation.CREATE)
        rendered = self._validate_values(values, create=True)
        columns = tuple(rendered)
        sql = (
            f"INSERT INTO {self._definition.table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)})"
        )
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, tuple(rendered.values()))

    async def update(self, values: dict[str, Any], *, where: dict[str, Any]) -> None:
        """Update rows selected by caller predicates plus the bound scope."""
        self._validate_operation(ResourceOperation.UPDATE)
        if not where:
            raise ValueError("update requires a non-empty where predicate")
        rendered = self._validate_values(values, create=False)
        predicates, where_params = self._where(where)
        assignments = ", ".join(f"{column} = ?" for column in rendered)
        sql = f"UPDATE {self._definition.table_name} SET {assignments} WHERE " + " AND ".join(
            predicates
        )
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, (*rendered.values(), *where_params))

    async def delete(self, *, where: dict[str, Any]) -> None:
        """Delete rows selected by caller predicates plus the bound scope."""
        self._validate_operation(ResourceOperation.DELETE)
        if not where:
            raise ValueError("delete requires a non-empty where predicate")
        predicates, params = self._where(where)
        sql = f"DELETE FROM {self._definition.table_name} WHERE " + " AND ".join(predicates)
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, tuple(params))


class ScopedTable(_StructuredTable):
    """A structured tenant-scoped table bound to one trusted scope context."""

    __slots__ = ("_context", "_privileged_tenant_wide")

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
        context: ScopeContext | TenantWideScopeContext,
        *,
        _privileged_tenant_wide: bool = False,
    ) -> None:
        if _privileged_tenant_wide:
            if type(context) is not TenantWideScopeContext:
                raise TypeError("privileged context must be a TenantWideScopeContext")
            definition = registry.validate_privileged_tenant_wide_binding(resource_name, context)
        else:
            definition = registry.validate_binding(resource_name, context)
        if definition.scope is ResourceScope.GLOBAL:
            raise ValueError("global resources require GlobalTable")
        super().__init__(database, registry, definition)
        self._context = context
        self._privileged_tenant_wide = _privileged_tenant_wide

    @classmethod
    def for_privileged_tenant_wide(
        cls,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
        context: TenantWideScopeContext,
    ) -> Self:
        """Construct the explicit privileged tenant-wide gateway."""
        return cls(
            database,
            registry,
            resource_name,
            context,
            _privileged_tenant_wide=True,
        )

    def _validate_operation(self, operation: ResourceOperation) -> None:
        if self._privileged_tenant_wide:
            assert type(self._context) is TenantWideScopeContext
            if operation is ResourceOperation.CREATE and self._definition.workspace_scoped:
                raise ValueError("workspace-scoped creates require a workspace context")
            self._registry.validate_privileged_tenant_wide_binding(
                self._definition.resource_name,
                self._context,
                operation=operation,
            )
        else:
            self._registry.validate_binding(
                self._definition.resource_name,
                self._context,
                operation=operation,
            )

    def _scope_items(self) -> tuple[tuple[str, str], ...]:
        items = [("tenant_id", self._context.tenant_id)]
        if self._definition.workspace_scoped and type(self._context) is ScopeContext:
            items.append(("workspace_id", self._context.workspace_id))
        return tuple(items)

    def _validate_join(self, other: ScopedTable) -> None:
        if (
            self._context != other._context
            or self._privileged_tenant_wide != other._privileged_tenant_wide
        ):
            raise ValueError("joined tables must use the same scope")
        if self._StructuredTable__database is not other._StructuredTable__database:
            raise ValueError("joined tables must use the same database")


class GlobalTable(_StructuredTable):
    """A structured gateway reserved for explicitly global reference tables."""

    __slots__ = ()

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
    ) -> None:
        definition = registry.validate_binding(resource_name, None)
        if definition.scope is not ResourceScope.GLOBAL:
            raise ValueError("tenant resources require ScopedTable")
        super().__init__(database, registry, definition)

    def _validate_operation(self, operation: ResourceOperation) -> None:
        self._registry.validate_binding(self._definition.resource_name, None, operation=operation)

    def _validate_values(self, values: dict[str, Any], *, create: bool) -> dict[str, Any]:
        rendered = super()._validate_values(values, create=create)
        if _OWNERSHIP_COLUMNS.intersection(rendered):
            raise ValueError("global resources cannot contain tenant or workspace ownership")
        return rendered

    def _where(
        self,
        where: dict[str, Any] | None,
        *,
        qualifier: str | None = None,
        include_scope: bool = True,
    ) -> tuple[list[str], list[Any]]:
        if where is not None and _OWNERSHIP_COLUMNS.intersection(where):
            raise ValueError("global resources cannot filter by tenant or workspace ownership")
        return super()._where(where, qualifier=qualifier, include_scope=include_scope)
