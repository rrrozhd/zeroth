"""Structured async SQL gateways that make resource scope unavoidable."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self

from zeroth.platform.storage.database import AsyncConnection, AsyncDatabase
from zeroth.platform.storage.scoping import (
    NullWorkspaceScopeContext,
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
    TenantWideScopeContext,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OWNERSHIP_COLUMNS = frozenset({"tenant_id", "workspace_id"})

ASYNC_PERSISTENCE_MODULES = frozenset(
    {
        "contracts/graph/repository.py",
        "contracts/graph/storage.py",
        "contracts/registry/registry.py",
        "governance/approvals/repository.py",
        "governance/attestations/store.py",
        "governance/audit/coordination.py",
        "governance/audit/repository.py",
        "governance/decisions/repository.py",
        "governance/retention/audit_log_repository.py",
        "governance/retention/claims.py",
        "governance/retention/cleanup_state_repository.py",
        "governance/retention/coordination.py",
        "governance/retention/legal_hold_repository.py",
        "governance/retention/policy_repository.py",
        "integrations/memory/config_repository.py",
        "integrations/persistence/runs/checkpoint_store.py",
        "integrations/persistence/runs/retention_queries.py",
        "integrations/persistence/runs/run_repository.py",
        "integrations/persistence/runs/thread_repository.py",
        "integrations/persistence/runs/token_snapshot_store.py",
        "platform/artifacts/store.py",
        "platform/secrets/vault.py",
        "runtime/agents/thread_store.py",
        "service/deployments/repository.py",
        "service/langgraph_gateway/enforcement_store.py",
        "service/webhooks/repository.py",
    }
)
"""Production persistence modules that must use structured storage gateways."""

ASYNC_NON_PERSISTENCE_MODULES = frozenset(
    {
        "contracts/templates/registry.py",
        "governance/policy/registry.py",
        "integrations/memory/registry.py",
    }
)
"""Persistence-shaped modules explicitly classified as in-memory metadata helpers."""

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
SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES = frozenset(
    {
        "quota_counters",
        "rate_limit_buckets",
        "retention_cleanup_operations",
        "side_effect_operations",
        "token_engine_snapshots",
        "webhook_dead_letters",
        "webhook_deliveries",
    }
)
"""Tenant resources awaiting their direct ownership migrations in Tasks 7-9."""

SERVICE_SCOPE_DEFINITIONS = tuple(
    ResourceScopeDefinition(
        resource_name=f"service.{table_name}",
        table_name=table_name,
        workspace_scoped=table_name in _SERVICE_WORKSPACE_TABLES,
        direct_scope_ready=table_name not in SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES,
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


def _definition_table_name(definition: ResourceScopeDefinition) -> str:
    try:
        return _identifier(definition.table_name)
    except ValueError as exc:
        raise ValueError("table_name must be a SQL identifier") from exc


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
    __slots__ = ("__database", "__registry", "__resource_name")

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
        canonical = registry.definition_for_resource(definition.resource_name)
        if canonical != definition:
            raise ValueError("definition must be the canonical registry definition")
        self.__database = database
        self.__registry = registry
        self.__resource_name = definition.resource_name
        _definition_table_name(definition)

    @property
    def _registry(self) -> ResourceScopeRegistry:
        return self.__registry

    @property
    def _definition(self) -> ResourceScopeDefinition:
        return self._canonical_definition()

    def _canonical_definition(self) -> ResourceScopeDefinition:
        return self.__registry.definition_for_resource(self.__resource_name)

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        raise NotImplementedError

    def _scope_items(
        self,
        definition: ResourceScopeDefinition,
    ) -> tuple[tuple[str, str | None], ...]:
        return ()

    def in_transaction(self, connection: AsyncConnection) -> BoundStructuredTable:
        """Bind this table's structural rules to an existing transaction."""
        return BoundStructuredTable(self, connection)

    @asynccontextmanager
    async def transaction(
        self,
        *,
        write_lock: bool = False,
    ) -> AsyncIterator[BoundStructuredTable]:
        """Open a transaction whose statements remain bound to this table's scope."""
        async with self.__database.transaction(write_lock=write_lock) as connection:
            yield BoundStructuredTable(self, connection)

    def _validate_values(
        self,
        values: dict[str, Any],
        *,
        create: bool,
        definition: ResourceScopeDefinition,
    ) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be a non-empty dict")
        rendered = {_identifier(key): value for key, value in values.items()}
        scope_items = dict(self._scope_items(definition))
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
        definition: ResourceScopeDefinition,
    ) -> tuple[list[str], list[Any]]:
        predicates: list[str] = []
        params: list[Any] = []
        for column, value in (where or {}).items():
            identifier = _identifier(column)
            rendered = f"{qualifier}.{identifier}" if qualifier else identifier
            if value is None:
                predicates.append(f"{rendered} IS NULL")
            else:
                predicates.append(f"{rendered} = ?")
                params.append(value)
        if include_scope:
            for column, value in self._scope_items(definition):
                rendered = f"{qualifier}.{column}" if qualifier else column
                if value is None:
                    predicates.append(f"{rendered} IS NULL")
                else:
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
        definition = self._validate_operation(
            ResourceOperation.ENUMERATE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        selected = _columns(columns, qualifier=table_name if joins else None)
        sql = f"SELECT {selected} FROM {table_name}"
        predicates, params = self._where(
            where,
            qualifier=table_name if joins else None,
            definition=definition,
        )
        for index, join in enumerate(joins, start=1):
            if type(join) is not ScopedJoin:
                raise TypeError("joins must contain exact ScopedJoin values")
            if type(join.table) is not ScopedTable:
                raise TypeError("join table must be an exact ScopedTable")
            _identifier(join.local_column)
            _identifier(join.foreign_column)
            if type(self) is not ScopedTable:
                raise ValueError("global tables cannot join through a scoped gateway")
            assert isinstance(self, ScopedTable)
            self._validate_join(join.table)
            joined_definition = join.table._validate_operation(
                ResourceOperation.ENUMERATE,
                join.table._canonical_definition(),
            )
            alias = f"j{index}"
            joined_name = _definition_table_name(joined_definition)
            sql += (
                f" JOIN {joined_name} AS {alias} ON "
                f"{table_name}.{join.local_column} = {alias}.{join.foreign_column}"
            )
            join_predicates, join_params = join.table._where(
                None,
                qualifier=alias,
                definition=joined_definition,
            )
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
        definition = self._validate_operation(
            ResourceOperation.READ,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        predicates, params = self._where(where, definition=definition)
        sql = (
            f"SELECT {_columns(columns)} FROM {table_name} WHERE "
            + " AND ".join(predicates)
            + " LIMIT 1"
        )
        async with self.__database.transaction() as connection:
            return await connection.fetch_one(sql, tuple(params))

    async def insert(self, values: dict[str, Any]) -> None:
        """Insert a row after filling and validating its ownership columns."""
        definition = self._validate_operation(
            ResourceOperation.CREATE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        rendered = self._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)})"
        )
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, tuple(rendered.values()))

    async def insert_if_absent(
        self,
        values: dict[str, Any],
        *,
        conflict_columns: tuple[str, ...],
    ) -> bool:
        """Insert a scoped row atomically, returning whether this call won the identity."""
        definition = self._validate_operation(
            ResourceOperation.CREATE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        rendered = self._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        conflicts = tuple(_identifier(column) for column in conflict_columns)
        if not conflicts or any(column not in rendered for column in conflicts):
            raise ValueError("conflict_columns must be non-empty inserted columns")
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT ({', '.join(conflicts)}) DO NOTHING "
            f"RETURNING {conflicts[0]}"
        )
        async with self.__database.transaction(write_lock=True) as connection:
            row = await connection.fetch_one(sql, tuple(rendered.values()))
        return row is not None

    async def update(self, values: dict[str, Any], *, where: dict[str, Any]) -> None:
        """Update rows selected by caller predicates plus the bound scope."""
        definition = self._validate_operation(
            ResourceOperation.UPDATE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("update requires a non-empty where predicate")
        rendered = self._validate_values(values, create=False, definition=definition)
        predicates, where_params = self._where(where, definition=definition)
        assignments = ", ".join(f"{column} = ?" for column in rendered)
        sql = f"UPDATE {table_name} SET {assignments} WHERE " + " AND ".join(predicates)
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, (*rendered.values(), *where_params))

    async def delete(self, *, where: dict[str, Any]) -> None:
        """Delete rows selected by caller predicates plus the bound scope."""
        definition = self._validate_operation(
            ResourceOperation.DELETE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("delete requires a non-empty where predicate")
        predicates, params = self._where(where, definition=definition)
        sql = f"DELETE FROM {table_name} WHERE " + " AND ".join(predicates)
        async with self.__database.transaction(write_lock=True) as connection:
            await connection.execute(sql, tuple(params))


class BoundStructuredTable:
    """A structured table view that keeps several operations in one transaction."""

    __slots__ = ("__connection", "__table")

    def __init__(self, table: _StructuredTable, connection: AsyncConnection) -> None:
        if not isinstance(table, _StructuredTable):
            raise TypeError("table must be a structured table")
        self.__table = table
        self.__connection = connection

    def bind(self, table: _StructuredTable) -> BoundStructuredTable:
        """Bind another table to this same transaction after database validation."""
        if not isinstance(table, _StructuredTable):
            raise TypeError("table must be a structured table")
        if (
            self.__table._StructuredTable__database  # noqa: SLF001
            is not table._StructuredTable__database  # noqa: SLF001
        ):
            raise ValueError("bound tables must use the same database")
        return BoundStructuredTable(table, self.__connection)

    def _definition(self, operation: ResourceOperation) -> ResourceScopeDefinition:
        definition = self.__table._canonical_definition()
        return self.__table._validate_operation(operation, definition)

    def _where(
        self,
        definition: ResourceScopeDefinition,
        where: dict[str, Any] | None,
        *,
        where_null: tuple[str, ...] = (),
        where_not_null: tuple[str, ...] = (),
        where_lt: dict[str, Any] | None = None,
        where_not_in: dict[str, tuple[Any, ...]] | None = None,
    ) -> tuple[list[str], list[Any]]:
        predicates, params = self.__table._where(where, definition=definition)
        for column in where_null:
            predicates.append(f"{_identifier(column)} IS NULL")
        for column in where_not_null:
            predicates.append(f"{_identifier(column)} IS NOT NULL")
        for column, value in (where_lt or {}).items():
            predicates.append(f"{_identifier(column)} < ?")
            params.append(value)
        for column, values in (where_not_in or {}).items():
            identifier = _identifier(column)
            if not values:
                continue
            predicates.append(f"{identifier} NOT IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        return predicates, params

    async def select(
        self,
        *,
        where: dict[str, Any] | None = None,
        columns: tuple[str, ...] = ("*",),
        where_null: tuple[str, ...] = (),
        where_not_null: tuple[str, ...] = (),
        where_lt: dict[str, Any] | None = None,
        where_not_in: dict[str, tuple[Any, ...]] | None = None,
        order_by: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Select rows using structured predicates inside this transaction."""
        definition = self._definition(ResourceOperation.ENUMERATE)
        table_name = _definition_table_name(definition)
        predicates, params = self._where(
            definition,
            where,
            where_null=where_null,
            where_not_null=where_not_null,
            where_lt=where_lt,
            where_not_in=where_not_in,
        )
        sql = f"SELECT {_columns(columns)} FROM {table_name}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        if order_by:
            sql += " ORDER BY " + ", ".join(_identifier(column) for column in order_by)
        return await self.__connection.fetch_all(sql, tuple(params))

    async def select_one(
        self,
        *,
        where: dict[str, Any],
        columns: tuple[str, ...] = ("*",),
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        """Select one scoped row, optionally acquiring a PostgreSQL row lock."""
        definition = self._definition(ResourceOperation.READ)
        table_name = _definition_table_name(definition)
        predicates, params = self._where(definition, where)
        sql = (
            f"SELECT {_columns(columns)} FROM {table_name} WHERE "
            + " AND ".join(predicates)
            + " LIMIT 1"
        )
        database = self.__table._StructuredTable__database  # noqa: SLF001
        if for_update and database.backend == "postgres":
            sql += " FOR UPDATE"
        return await self.__connection.fetch_one(sql, tuple(params))

    async def insert(self, values: dict[str, Any]) -> None:
        """Insert a scoped row inside this transaction."""
        definition = self._definition(ResourceOperation.CREATE)
        table_name = _definition_table_name(definition)
        rendered = self.__table._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)})"
        )
        await self.__connection.execute(sql, tuple(rendered.values()))

    async def insert_if_absent(
        self,
        values: dict[str, Any],
        *,
        conflict_columns: tuple[str, ...],
    ) -> bool:
        """Insert once under a structured conflict identity."""
        definition = self._definition(ResourceOperation.CREATE)
        table_name = _definition_table_name(definition)
        rendered = self.__table._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        conflicts = tuple(_identifier(column) for column in conflict_columns)
        if not conflicts or any(column not in rendered for column in conflicts):
            raise ValueError("conflict_columns must be non-empty inserted columns")
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT ({', '.join(conflicts)}) DO NOTHING "
            f"RETURNING {conflicts[0]}"
        )
        row = await self.__connection.fetch_one(sql, tuple(rendered.values()))
        return row is not None

    async def update(self, values: dict[str, Any], *, where: dict[str, Any]) -> None:
        """Update scoped rows inside this transaction."""
        definition = self._definition(ResourceOperation.UPDATE)
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("update requires a non-empty where predicate")
        rendered = self.__table._validate_values(values, create=False, definition=definition)
        predicates, where_params = self._where(definition, where)
        assignments = ", ".join(f"{column} = ?" for column in rendered)
        await self.__connection.execute(
            f"UPDATE {table_name} SET {assignments} WHERE " + " AND ".join(predicates),
            (*rendered.values(), *where_params),
        )


class ScopedTable(_StructuredTable):
    """A structured tenant-scoped table bound to one trusted scope context."""

    __slots__ = ("__context", "__privileged_tenant_wide")

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
        context: ScopeContext | NullWorkspaceScopeContext | TenantWideScopeContext,
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
        self.__context = context
        self.__privileged_tenant_wide = _privileged_tenant_wide

    @property
    def _context(self) -> ScopeContext | NullWorkspaceScopeContext | TenantWideScopeContext:
        return self.__context

    @property
    def _privileged_tenant_wide(self) -> bool:
        return self.__privileged_tenant_wide

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

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        if self._privileged_tenant_wide:
            assert type(self._context) is TenantWideScopeContext
            if operation is ResourceOperation.CREATE and definition.workspace_scoped:
                raise ValueError("workspace-scoped creates require a workspace context")
            return self._registry.validate_privileged_tenant_wide_binding(
                definition.resource_name,
                self._context,
                operation=operation,
            )
        return self._registry.validate_binding(
            definition.resource_name,
            self._context,
            operation=operation,
        )

    def _scope_items(
        self,
        definition: ResourceScopeDefinition,
    ) -> tuple[tuple[str, str | None], ...]:
        items = [("tenant_id", self._context.tenant_id)]
        if definition.workspace_scoped and type(self._context) is ScopeContext:
            items.append(("workspace_id", self._context.workspace_id))
        elif definition.workspace_scoped and type(self._context) is NullWorkspaceScopeContext:
            items.append(("workspace_id", None))
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

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        return self._registry.validate_binding(
            definition.resource_name,
            None,
            operation=operation,
        )

    def _validate_values(
        self,
        values: dict[str, Any],
        *,
        create: bool,
        definition: ResourceScopeDefinition,
    ) -> dict[str, Any]:
        rendered = super()._validate_values(
            values,
            create=create,
            definition=definition,
        )
        if _OWNERSHIP_COLUMNS.intersection(rendered):
            raise ValueError("global resources cannot contain tenant or workspace ownership")
        return rendered

    def _where(
        self,
        where: dict[str, Any] | None,
        *,
        qualifier: str | None = None,
        include_scope: bool = True,
        definition: ResourceScopeDefinition,
    ) -> tuple[list[str], list[Any]]:
        if where is not None and _OWNERSHIP_COLUMNS.intersection(where):
            raise ValueError("global resources cannot filter by tenant or workspace ownership")
        return super()._where(
            where,
            qualifier=qualifier,
            include_scope=include_scope,
            definition=definition,
        )
