"""Structured async SQL gateways that make resource scope unavoidable."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from zeroth.platform.storage.database import (
    AsyncConnection,
    AsyncDatabase,
    database_now,
    database_now_text_expression,
)
from zeroth.platform.storage.scoping import (
    CrossTenantMaintenanceScopeContext,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
    TenantWideScopeContext,
    persistence_operation,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OWNERSHIP_COLUMNS = frozenset({"tenant_id", "workspace_id", "workspace_scope"})

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
        "governance/guardrails/policy.py",
        "governance/retention/audit_log_repository.py",
        "governance/retention/claims.py",
        "governance/retention/cleanup_state_repository.py",
        "governance/retention/coordination.py",
        "governance/retention/legal_hold_repository.py",
        "governance/retention/policy_repository.py",
        "governance/retention/workspace_reader.py",
        "integrations/memory/config_repository.py",
        "integrations/persistence/runs/checkpoint_store.py",
        "integrations/persistence/runs/retention_queries.py",
        "integrations/persistence/runs/run_repository.py",
        "integrations/persistence/runs/thread_repository.py",
        "integrations/persistence/runs/token_snapshot_store.py",
        "platform/artifacts/store.py",
        "platform/secrets/vault.py",
        "runtime/agents/thread_store.py",
        "service/certifications/repository.py",
        "service/deployments/repository.py",
        "service/github/repository.py",
        "service/langgraph_gateway/enforcement_store.py",
        "service/repositories/repository.py",
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
    "app_certification_events",
    "app_certifications",
    "approvals",
    "audit_chain_heads",
    "contract_versions",
    "decision_records",
    "deployment_versions",
    "enforcement_heartbeats",
    "github_installations",
    "github_repositories",
    "github_webhook_deliveries",
    "graph_versions",
    "guardrail_admission_state",
    "guardrail_policy_revisions",
    "langgraph_decisions",
    "langgraph_inventories",
    "langgraph_run_attestations",
    "legal_holds",
    "mcp_server_configs",
    "memory_connector_configs",
    "node_audits",
    "prompt_templates",
    "template_dependency_references",
    "quota_counters",
    "rate_limit_buckets",
    "repo_checkouts",
    "repo_runs",
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
        "app_certification_events",
        "app_certifications",
        "approvals",
        "deployment_versions",
        "graph_versions",
        "guardrail_admission_state",
        "node_audits",
        "prompt_templates",
        "template_dependency_references",
        "repo_checkouts",
        "repo_runs",
        "run_checkpoints",
        "runs",
        "side_effect_operations",
        "threads",
        "token_engine_snapshots",
    }
)
_DERIVED_WORKSPACE_SCOPE_TABLES = frozenset(
    {
        "prompt_templates",
        "guardrail_admission_state",
        "run_checkpoints",
        "runs",
        "side_effect_operations",
        "threads",
        "token_engine_snapshots",
        "template_dependency_references",
    }
)
_TASK9_RESOURCE_OPERATIONS = {
    # Append-only timeline with no single-event read: CertificationRepository
    # appends via create/grant_override and returns whole timelines via events().
    # READ had no implementing method, so the leak matrix demanded a probe for an
    # operation the resource does not offer.
    "app_certification_events": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.ENUMERATE}
    ),
    "app_certifications": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "approvals": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "audit_chain_heads": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE}
    ),
    "contract_versions": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.DELETE,
        }
    ),
    "decision_records": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "deployment_versions": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "enforcement_heartbeats": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "github_installations": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "github_repositories": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "github_webhook_deliveries": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.ENUMERATE, ResourceOperation.DELETE}
    ),
    "graph_versions": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "guardrail_policy_revisions": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.ENUMERATE}
    ),
    "guardrail_admission_state": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "memory_connector_configs": frozenset(ResourceOperation),
    "node_audits": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "prompt_templates": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.DELETE,
        }
    ),
    # Derived rows, never edited in place: TemplateReferenceIndex.rebuild()
    # deletes and re-inserts, and no method updates a reference. Declaring
    # UPDATE made the leak matrix demand a probe for an operation that does
    # not exist, which no repository could ever satisfy.
    "template_dependency_references": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.DELETE,
        }
    ),
    "run_attestations": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "tool_inventory_registrations": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "quota_counters": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE}
    ),
    "rate_limit_buckets": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE}
    ),
    "repo_checkouts": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "repo_runs": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "retention_audit_log": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.ENUMERATE}
    ),
    "retention_cleanup_operations": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "retention_cleanup_state": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.UPDATE,
        }
    ),
    "retention_coordination": frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    "retention_policies": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "legal_holds": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "run_checkpoints": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
            ResourceOperation.DELETE,
        }
    ),
    "runs": frozenset(ResourceOperation),
    "threads": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "token_engine_snapshots": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.UPDATE,
            ResourceOperation.DELETE,
        }
    ),
    "webhook_subscriptions": frozenset(ResourceOperation),
    "webhook_deliveries": frozenset(
        {
            ResourceOperation.CREATE,
            ResourceOperation.READ,
            ResourceOperation.ENUMERATE,
            ResourceOperation.UPDATE,
        }
    ),
    "webhook_dead_letters": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.ENUMERATE}
    ),
    "langgraph_decisions": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.ENUMERATE}
    ),
    "langgraph_inventories": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE}
    ),
    "langgraph_run_attestations": frozenset(
        {ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.ENUMERATE}
    ),
}
SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES = frozenset()
"""Tenant resources awaiting a direct ownership migration."""

SERVICE_SCOPE_DEFINITIONS = tuple(
    ResourceScopeDefinition(
        resource_name=f"service.{table_name}",
        table_name=table_name,
        workspace_scoped=table_name in _SERVICE_WORKSPACE_TABLES,
        direct_scope_ready=table_name not in SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES,
        operations=_TASK9_RESOURCE_OPERATIONS.get(table_name, frozenset(ResourceOperation)),
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
        table_name="alembic_version_econ",
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
    """Resolve identifier for structurally scoped persistence."""
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return value


def _definition_table_name(definition: ResourceScopeDefinition) -> str:
    """Resolve definition table name for structurally scoped persistence."""
    try:
        return _identifier(definition.table_name)
    except ValueError as exc:
        raise ValueError("table_name must be a SQL identifier") from exc


def _columns(values: tuple[str, ...], *, qualifier: str | None = None) -> str:
    """Resolve columns for structurally scoped persistence."""
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
        """Validate the immutable scope definition after initialization."""
        if type(self.table) is not ScopedTable:
            raise TypeError("join table must be a ScopedTable")
        _identifier(self.local_column)
        _identifier(self.foreign_column)


class _StructuredTable:
    """Represent StructuredTable within the structural tenant-isolation boundary."""

    __slots__ = ("__database", "__registry", "__resource_name")

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        definition: ResourceScopeDefinition,
    ) -> None:
        """Bind the repository or gateway to its validated scope."""
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
        """Resolve registry for structurally scoped persistence."""
        return self.__registry

    @property
    def _definition(self) -> ResourceScopeDefinition:
        """Resolve definition for structurally scoped persistence."""
        return self._canonical_definition()

    def _canonical_definition(self) -> ResourceScopeDefinition:
        """Resolve canonical definition for structurally scoped persistence."""
        return self.__registry.definition_for_resource(self.__resource_name)

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        """Validate operation against the bound resource scope."""
        raise NotImplementedError

    def _scope_items(
        self,
        definition: ResourceScopeDefinition,
    ) -> tuple[tuple[str, str | None], ...]:
        """Resolve scope items for structurally scoped persistence."""
        return ()

    def _transaction_scope_identity(self) -> tuple[object, ...]:
        """Return the structural authority carried by a bound transaction."""
        return (type(self),)

    def _accepts_transaction_scope_from(self, source: _StructuredTable) -> bool:
        """Resolve accepts transaction scope from for structurally scoped persistence."""
        return self._transaction_scope_identity() == source._transaction_scope_identity()

    def in_transaction(
        self, connection: AsyncConnection | BoundStructuredTable
    ) -> BoundStructuredTable:
        """Bind this table's structural rules to an existing transaction."""
        if isinstance(connection, BoundStructuredTable):
            return connection.bind(self)
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
        """Validate values against the bound resource scope."""
        if not isinstance(values, dict):
            raise TypeError("values must be a dict")
        if not values and not create:
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
        """Resolve where for structurally scoped persistence."""
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

    @persistence_operation(ResourceOperation.ENUMERATE)
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

    @persistence_operation(ResourceOperation.READ)
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

    @persistence_operation(ResourceOperation.CREATE)
    async def insert(self, values: dict[str, Any]) -> dict[str, Any]:
        """Insert and return the complete physical row, including generated values."""
        definition = self._validate_operation(
            ResourceOperation.CREATE,
            self._canonical_definition(),
        )
        table_name = _definition_table_name(definition)
        rendered = self._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) RETURNING *"
            if columns
            else f"INSERT INTO {table_name} DEFAULT VALUES RETURNING *"
        )
        async with self.__database.transaction(write_lock=True) as connection:
            row = await connection.fetch_one(sql, tuple(rendered.values()))
        if row is None:
            raise RuntimeError("insert did not return the created row")
        return row

    @persistence_operation(ResourceOperation.CREATE)
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

    @persistence_operation(ResourceOperation.UPDATE)
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

    @persistence_operation(ResourceOperation.DELETE)
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
        """Bind the repository or gateway to its validated scope."""
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
        if not table._accepts_transaction_scope_from(self.__table):
            raise ValueError("bound tables must use the same structural scope")
        return BoundStructuredTable(table, self.__connection)

    async def _database_now(self) -> datetime:
        """Return authoritative statement-time from this bound transaction."""
        database = self.__table._StructuredTable__database  # noqa: SLF001
        return await database_now(self.__connection, database.backend)

    def _definition(self, operation: ResourceOperation) -> ResourceScopeDefinition:
        """Resolve definition for structurally scoped persistence."""
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
        where_gte_database_now: tuple[str, ...] = (),
        where_in: dict[str, tuple[Any, ...]] | None = None,
        where_not_in: dict[str, tuple[Any, ...]] | None = None,
    ) -> tuple[list[str], list[Any]]:
        """Resolve where for structurally scoped persistence."""
        predicates, params = self.__table._where(where, definition=definition)
        for column in where_null:
            predicates.append(f"{_identifier(column)} IS NULL")
        for column in where_not_null:
            predicates.append(f"{_identifier(column)} IS NOT NULL")
        for column, value in (where_lt or {}).items():
            predicates.append(f"{_identifier(column)} < ?")
            params.append(value)
        if where_gte_database_now:
            database = self.__table._StructuredTable__database  # noqa: SLF001
            now = database_now_text_expression(database.backend)
            predicates.extend(
                f"{_identifier(column)} >= {now}" for column in where_gte_database_now
            )
        for column, values in (where_in or {}).items():
            identifier = _identifier(column)
            if not values:
                predicates.append("1 = 0")
                continue
            predicates.append(f"{identifier} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        for column, values in (where_not_in or {}).items():
            identifier = _identifier(column)
            if not values:
                continue
            predicates.append(f"{identifier} NOT IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        return predicates, params

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def select(
        self,
        *,
        where: dict[str, Any] | None = None,
        columns: tuple[str, ...] = ("*",),
        where_null: tuple[str, ...] = (),
        where_not_null: tuple[str, ...] = (),
        where_lt: dict[str, Any] | None = None,
        where_in: dict[str, tuple[Any, ...]] | None = None,
        where_not_in: dict[str, tuple[Any, ...]] | None = None,
        order_by: tuple[str, ...] = (),
        order_by_desc: tuple[str, ...] = (),
        limit: int | None = None,
        offset: int = 0,
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
            where_in=where_in,
            where_not_in=where_not_in,
        )
        sql = f"SELECT {_columns(columns)} FROM {table_name}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        if order_by:
            sql += " ORDER BY " + ", ".join(_identifier(column) for column in order_by)
        elif order_by_desc:
            sql += " ORDER BY " + ", ".join(
                f"{_identifier(column)} DESC" for column in order_by_desc
            )
        if limit is not None:
            if type(limit) is not int or limit < 0:
                raise ValueError("limit must be a non-negative int")
            if type(offset) is not int or offset < 0:
                raise ValueError("offset must be a non-negative int")
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        return await self.__connection.fetch_all(sql, tuple(params))

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def count(self, *, where: dict[str, Any] | None = None) -> int:
        """Count scoped rows without materializing their identities."""
        definition = self._definition(ResourceOperation.ENUMERATE)
        table_name = _definition_table_name(definition)
        predicates, params = self._where(definition, where)
        sql = f"SELECT COUNT(*) AS row_count FROM {table_name}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        row = await self.__connection.fetch_one(sql, tuple(params))
        return 0 if row is None else int(row["row_count"])

    @persistence_operation(ResourceOperation.READ)
    async def select_one(
        self,
        *,
        where: dict[str, Any],
        columns: tuple[str, ...] = ("*",),
        for_update: bool = False,
        order_by: tuple[str, ...] = (),
        order_by_desc: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Select one scoped row, with optional deterministic ordering and row lock."""
        if order_by and order_by_desc:
            raise ValueError("order_by and order_by_desc are mutually exclusive")
        definition = self._definition(ResourceOperation.READ)
        table_name = _definition_table_name(definition)
        predicates, params = self._where(definition, where)
        sql = f"SELECT {_columns(columns)} FROM {table_name} WHERE " + " AND ".join(predicates)
        if order_by:
            sql += " ORDER BY " + ", ".join(_identifier(column) for column in order_by)
        elif order_by_desc:
            sql += " ORDER BY " + ", ".join(
                f"{_identifier(column)} DESC" for column in order_by_desc
            )
        sql += " LIMIT 1"
        database = self.__table._StructuredTable__database  # noqa: SLF001
        if for_update and database.backend == "postgres":
            sql += " FOR UPDATE"
        return await self.__connection.fetch_one(sql, tuple(params))

    @persistence_operation(ResourceOperation.CREATE)
    async def insert(self, values: dict[str, Any]) -> dict[str, Any]:
        """Insert and return a complete scoped row inside this transaction."""
        definition = self._definition(ResourceOperation.CREATE)
        table_name = _definition_table_name(definition)
        rendered = self.__table._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) RETURNING *"
            if columns
            else f"INSERT INTO {table_name} DEFAULT VALUES RETURNING *"
        )
        row = await self.__connection.fetch_one(sql, tuple(rendered.values()))
        if row is None:
            raise RuntimeError("insert did not return the created row")
        return row

    @persistence_operation(ResourceOperation.CREATE)
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

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.UPDATE)
    async def upsert(
        self,
        values: dict[str, Any],
        *,
        conflict_columns: tuple[str, ...],
        update_columns: tuple[str, ...],
        returning: str,
        update_where: dict[str, Any] | None = None,
    ) -> bool:
        """Insert or update a scoped row, preserving an optional atomic fence."""
        definition = self._definition(ResourceOperation.CREATE)
        self._definition(ResourceOperation.UPDATE)
        table_name = _definition_table_name(definition)
        rendered = self.__table._validate_values(values, create=True, definition=definition)
        columns = tuple(rendered)
        conflicts = tuple(_identifier(column) for column in conflict_columns)
        updates = tuple(_identifier(column) for column in update_columns)
        if not conflicts or any(column not in rendered for column in conflicts):
            raise ValueError("conflict_columns must be non-empty inserted columns")
        if not updates or any(column not in rendered for column in updates):
            raise ValueError("update_columns must be non-empty inserted columns")
        if _OWNERSHIP_COLUMNS.intersection(updates):
            raise ValueError("ownership columns cannot be updated")
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT ({', '.join(conflicts)}) DO UPDATE SET "
            + ", ".join(f"{column} = excluded.{column}" for column in updates)
        )
        params = list(rendered.values())
        if update_where:
            clauses: list[str] = []
            for column, value in update_where.items():
                identifier = _identifier(column)
                if value is None:
                    clauses.append(f"{table_name}.{identifier} IS NULL")
                else:
                    clauses.append(f"{table_name}.{identifier} = ?")
                    params.append(value)
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" RETURNING {_identifier(returning)}"
        row = await self.__connection.fetch_one(sql, tuple(params))
        return row is not None

    @persistence_operation(ResourceOperation.UPDATE)
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

    @persistence_operation(ResourceOperation.UPDATE)
    async def update_if_matches(
        self,
        values: dict[str, Any],
        *,
        where: dict[str, Any],
        returning: str,
        where_gte_database_now: tuple[str, ...] = (),
        where_not_null: tuple[str, ...] = (),
        where_not_in: dict[str, tuple[Any, ...]] | None = None,
        increment: tuple[str, ...] = (),
    ) -> bool:
        """Atomically update a scoped row and report whether predicates matched."""
        definition = self._definition(ResourceOperation.UPDATE)
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("update requires a non-empty where predicate")
        rendered = self.__table._validate_values(values, create=False, definition=definition)
        predicates, where_params = self._where(
            definition,
            where,
            where_gte_database_now=where_gte_database_now,
            where_not_null=where_not_null,
            where_not_in=where_not_in,
        )
        increments = tuple(_identifier(column) for column in increment)
        if _OWNERSHIP_COLUMNS.intersection(increments):
            raise ValueError("ownership columns cannot be updated")
        assignments = [f"{column} = ?" for column in rendered]
        assignments.extend(f"{column} = {column} + 1" for column in increments)
        row = await self.__connection.fetch_one(
            f"UPDATE {table_name} SET {', '.join(assignments)} WHERE "
            + " AND ".join(predicates)
            + f" RETURNING {_identifier(returning)}",
            (*rendered.values(), *where_params),
        )
        return row is not None

    @persistence_operation(ResourceOperation.UPDATE)
    async def increment_and_get(
        self,
        column: str,
        *,
        where: dict[str, Any],
    ) -> int | None:
        """Atomically increment an integer column on a scoped row."""
        definition = self._definition(ResourceOperation.UPDATE)
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("increment requires a non-empty where predicate")
        identifier = _identifier(column)
        if identifier in _OWNERSHIP_COLUMNS:
            raise ValueError("ownership columns cannot be updated")
        predicates, params = self._where(definition, where)
        row = await self.__connection.fetch_one(
            f"UPDATE {table_name} SET {identifier} = {identifier} + 1 WHERE "
            + " AND ".join(predicates)
            + f" RETURNING {identifier}",
            tuple(params),
        )
        return None if row is None else int(row[identifier])

    @persistence_operation(ResourceOperation.DELETE)
    async def delete(self, *, where: dict[str, Any]) -> None:
        """Delete scoped rows inside this transaction."""
        definition = self._definition(ResourceOperation.DELETE)
        table_name = _definition_table_name(definition)
        if not where:
            raise ValueError("delete requires a non-empty where predicate")
        predicates, params = self._where(definition, where)
        await self.__connection.execute(
            f"DELETE FROM {table_name} WHERE " + " AND ".join(predicates),
            tuple(params),
        )


class ScopedTable(_StructuredTable):
    """A structured tenant-scoped table bound to one trusted scope context."""

    __slots__ = ("__context", "__privileged_tenant_wide", "__cross_tenant_maintenance")

    def __init__(
        self,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
        context: (
            ScopeContext
            | NullWorkspaceScopeContext
            | TenantWideScopeContext
            | CrossTenantMaintenanceScopeContext
        ),
        *,
        _privileged_tenant_wide: bool = False,
        _cross_tenant_maintenance: bool = False,
    ) -> None:
        """Bind the repository or gateway to its validated scope."""
        if _cross_tenant_maintenance:
            if type(context) is not CrossTenantMaintenanceScopeContext:
                raise TypeError("maintenance context must be a CrossTenantMaintenanceScopeContext")
            definition = registry.validate_cross_tenant_maintenance_binding(
                resource_name, context, operation=ResourceOperation.ENUMERATE
            )
        elif _privileged_tenant_wide:
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
        self.__cross_tenant_maintenance = _cross_tenant_maintenance

    @property
    def _context(self) -> object:
        """Resolve context for structurally scoped persistence."""
        return self.__context

    @property
    def _privileged_tenant_wide(self) -> bool:
        """Resolve privileged tenant wide for structurally scoped persistence."""
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

    @classmethod
    def for_cross_tenant_maintenance(
        cls,
        database: AsyncDatabase,
        registry: ResourceScopeRegistry,
        resource_name: str,
        context: CrossTenantMaintenanceScopeContext,
    ) -> Self:
        """Create or resolve for cross tenant maintenance for structurally scoped persistence."""
        return cls(
            database,
            registry,
            resource_name,
            context,
            _cross_tenant_maintenance=True,
        )

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        """Validate operation against the bound resource scope."""
        if self.__cross_tenant_maintenance:
            assert type(self._context) is CrossTenantMaintenanceScopeContext
            return self._registry.validate_cross_tenant_maintenance_binding(
                definition.resource_name, self._context, operation=operation
            )
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
        """Resolve scope items for structurally scoped persistence."""
        if self.__cross_tenant_maintenance:
            return ()
        assert not isinstance(self._context, CrossTenantMaintenanceScopeContext)
        items = [("tenant_id", self._context.tenant_id)]
        if definition.workspace_scoped and type(self._context) is ScopeContext:
            items.append(("workspace_id", self._context.workspace_id))
            if definition.table_name in _DERIVED_WORKSPACE_SCOPE_TABLES:
                items.append(("workspace_scope", f"value:{self._context.workspace_id}"))
        elif definition.workspace_scoped and type(self._context) is NullWorkspaceScopeContext:
            items.append(("workspace_id", None))
            if definition.table_name in _DERIVED_WORKSPACE_SCOPE_TABLES:
                items.append(("workspace_scope", "null"))
        return tuple(items)

    def _transaction_scope_identity(self) -> tuple[object, ...]:
        """Resolve transaction scope identity for structurally scoped persistence."""
        return (
            type(self._context),
            self._context,
            self.__privileged_tenant_wide,
            self.__cross_tenant_maintenance,
        )

    def _accepts_transaction_scope_from(self, source: _StructuredTable) -> bool:
        """Resolve accepts transaction scope from for structurally scoped persistence."""
        if super()._accepts_transaction_scope_from(source):
            return True
        if type(source) is not ScopedTable:
            return False
        assert isinstance(source, ScopedTable)
        return (
            not self._definition.workspace_scoped
            and not self.__privileged_tenant_wide
            and not self.__cross_tenant_maintenance
            and not source.__privileged_tenant_wide
            and not source.__cross_tenant_maintenance
            and type(self._context) is NullWorkspaceScopeContext
            and type(source._context) is ScopeContext
            and self._context.tenant_id == source._context.tenant_id
        )

    def _validate_join(self, other: ScopedTable) -> None:
        """Validate join against the bound resource scope."""
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
        """Bind the repository or gateway to its validated scope."""
        definition = registry.validate_binding(resource_name, None)
        if definition.scope is not ResourceScope.GLOBAL:
            raise ValueError("tenant resources require ScopedTable")
        super().__init__(database, registry, definition)

    def _validate_operation(
        self,
        operation: ResourceOperation,
        definition: ResourceScopeDefinition,
    ) -> ResourceScopeDefinition:
        """Validate operation against the bound resource scope."""
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
        """Validate values against the bound resource scope."""
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
        """Resolve where for structurally scoped persistence."""
        if where is not None and _OWNERSHIP_COLUMNS.intersection(where):
            raise ValueError("global resources cannot filter by tenant or workspace ownership")
        return super()._where(
            where,
            qualifier=qualifier,
            include_scope=include_scope,
            definition=definition,
        )
