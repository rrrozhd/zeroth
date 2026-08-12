"""Registry-derived fixtures for the cross-tenant persistence matrix."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy import delete, inspect as sa_inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage import (
    NullWorkspaceScopeContext,
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
    ScopedTable,
)

type OperationDriver = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReflectedColumn:
    name: str
    sql_type: str
    nullable: bool
    default: str | None
    primary_key_order: int


@dataclass(frozen=True, slots=True)
class ReflectedForeignKey:
    parent_table: str
    columns: tuple[str, ...]
    parent_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReflectedTable:
    name: str
    columns: tuple[ReflectedColumn, ...]
    foreign_keys: tuple[ReflectedForeignKey, ...]


@dataclass(frozen=True, slots=True)
class CrossTenantCase:
    definition: ResourceScopeDefinition
    operation: ResourceOperation
    mapped_model: type[Any] | None = None

    @property
    def parameter_id(self) -> str:
        return f"{self.definition.resource_name}:{self.operation.value}"


def generated_cross_tenant_cases(
    registry: ResourceScopeRegistry,
    physical_table_names: Iterable[str],
) -> tuple[CrossTenantCase, ...]:
    """Return every operation for every physical tenant resource."""
    validate_resource_inventory(registry, physical_table_names)
    return tuple(
        CrossTenantCase(definition, operation)
        for definition in sorted(registry.definitions, key=lambda item: item.resource_name)
        if definition.scope is ResourceScope.TENANT_SCOPED and definition.direct_scope_ready
        for operation in sorted(definition.operations, key=lambda item: item.value)
    )


def generated_sqlalchemy_cases(
    mapped_models: Iterable[type[Any]], engine: Engine
) -> tuple[CrossTenantCase, ...]:
    """Generate operation cases from physical SQLAlchemy mappings."""
    models = tuple(mapped_models)
    definitions = tuple(model.scope_definition for model in models)
    registry = ResourceScopeRegistry(definitions)
    physical = frozenset(sa_inspect(engine).get_table_names())
    validate_resource_inventory(registry, physical)
    by_resource = {model.scope_definition.resource_name: model for model in models}
    return tuple(
        CrossTenantCase(case.definition, case.operation, by_resource[case.definition.resource_name])
        for case in generated_cross_tenant_cases(registry, physical)
    )


def validate_resource_inventory(
    registry: ResourceScopeRegistry,
    physical_table_names: Iterable[str],
) -> None:
    """Reject logical definitions without a matching physical resource."""
    physical = frozenset(physical_table_names)
    missing = {
        definition.resource_name
        for definition in registry.definitions
        if definition.scope is ResourceScope.TENANT_SCOPED
        and definition.direct_scope_ready
        and definition.table_name not in physical
    }
    if missing:
        raise AssertionError(f"missing physical tenant resources: {sorted(missing)}")


def immutable_operation_map(
    operations: Mapping[ResourceOperation, OperationDriver],
) -> Mapping[ResourceOperation, OperationDriver]:
    """Validate and freeze a production resource driver's operation map."""
    return MappingProxyType(dict(operations))


async def exercise_relational_case(
    database: Any,
    registry: ResourceScopeRegistry,
    case: CrossTenantCase,
) -> None:
    """Exercise one registry-generated physical-table operation."""
    definition = case.definition
    owner_id = "matrix-owner"
    foreign_id = "matrix-foreign"
    owner_context = (
        ScopeContext(owner_id, "matrix-workspace")
        if definition.workspace_scoped
        else NullWorkspaceScopeContext(owner_id)
    )
    foreign_context = (
        ScopeContext(foreign_id, "matrix-workspace")
        if definition.workspace_scoped
        else NullWorkspaceScopeContext(foreign_id)
    )
    owner = ScopedTable(database, registry, definition.resource_name, owner_context)
    foreign = ScopedTable(database, registry, definition.resource_name, foreign_context)
    seeded: dict[tuple[str, str], dict[str, Any]] = {}
    owner_values = await seed_scoped_resource(
        database, registry, definition.resource_name, owner_context, token="owner", seeded=seeded
    )
    schema = await reflect_table(database, definition.table_name)
    identity = {
        column.name: owner_values[column.name]
        for column in schema.columns
        if column.primary_key_order and column.name not in {"tenant_id", "workspace_scope"}
    }
    if not identity:
        identity = {
            column.name: owner_values[column.name]
            for column in schema.columns
            if column.name not in {"tenant_id", "workspace_id", "workspace_scope"}
        }
        identity = dict(tuple(identity.items())[:1])
    owner_row = await owner.select_one(where=identity)
    assert owner_row is not None

    if case.operation is ResourceOperation.CREATE:
        await seed_scoped_resource(
            database,
            registry,
            definition.resource_name,
            foreign_context,
            token="foreign",
            seeded=seeded,
        )
        assert await owner.select_one(where=identity) is not None
    elif case.operation is ResourceOperation.READ:
        assert await foreign.select_one(where=identity) is None
    elif case.operation is ResourceOperation.ENUMERATE:
        assert await foreign.select() == []
    elif case.operation is ResourceOperation.UPDATE:
        update_column = next(
            column
            for column in schema.columns
            if not column.primary_key_order
            and column.name not in {"tenant_id", "workspace_id", "workspace_scope"}
            and column.name in owner_row
        )
        original = owner_row[update_column.name]
        await foreign.update(
            {update_column.name: _different_value(update_column, original)}, where=identity
        )
        assert (await owner.select_one(where=identity) or {})[update_column.name] == original
    else:
        await foreign.delete(where=identity)
        assert await owner.select_one(where=identity) is not None


async def physical_table_names(database: Any) -> frozenset[str]:
    async with database.transaction() as connection:
        rows = await connection.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    return frozenset(str(row["name"]) for row in rows)


async def reflect_table(database: Any, table_name: str) -> ReflectedTable:
    async with database.transaction() as connection:
        columns = await connection.fetch_all(f"PRAGMA table_info({table_name})")
        foreign_rows = await connection.fetch_all(f"PRAGMA foreign_key_list({table_name})")
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in foreign_rows:
        grouped.setdefault(int(row["id"]), []).append(row)
    foreign_keys = tuple(
        ReflectedForeignKey(
            parent_table=str(rows[0]["table"]),
            columns=tuple(str(row["from"]) for row in sorted(rows, key=lambda item: item["seq"])),
            parent_columns=tuple(
                str(row["to"]) for row in sorted(rows, key=lambda item: item["seq"])
            ),
        )
        for rows in grouped.values()
    )
    return ReflectedTable(
        name=table_name,
        columns=tuple(
            ReflectedColumn(
                name=str(row["name"]),
                sql_type=str(row["type"]),
                nullable=not bool(row["notnull"]),
                default=None if row["dflt_value"] is None else str(row["dflt_value"]),
                primary_key_order=int(row["pk"]),
            )
            for row in columns
        ),
        foreign_keys=foreign_keys,
    )


async def seed_scoped_resource(
    database: Any,
    registry: ResourceScopeRegistry,
    resource_name: str,
    context: ScopeContext | NullWorkspaceScopeContext,
    *,
    token: str,
    seeded: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create one valid row from physical schema metadata, including FK parents."""
    cache = {} if seeded is None else seeded
    cache_key = (resource_name, f"{context.tenant_id}:{token}")
    if cache_key in cache:
        return cache[cache_key]
    definition = registry.definition_for_resource(resource_name)
    schema = await reflect_table(database, definition.table_name)
    values: dict[str, Any] = {}
    cache[cache_key] = values
    for foreign_key in schema.foreign_keys:
        parent = registry.definition_for_table(foreign_key.parent_table)
        parent_values = await seed_scoped_resource(
            database, registry, parent.resource_name, context, token=token, seeded=cache
        )
        values.update(
            dict(
                zip(
                    foreign_key.columns,
                    (parent_values[name] for name in foreign_key.parent_columns),
                    strict=True,
                )
            )
        )
    for column in schema.columns:
        if column.name in {"tenant_id", "workspace_id", "workspace_scope"}:
            continue
        if (
            column.name in values
            or column.default is not None
            or (column.nullable and not column.primary_key_order)
        ):
            continue
        values[column.name] = _column_value(column, token)
    await ScopedTable(database, registry, resource_name, context).insert(values)
    values["tenant_id"] = context.tenant_id
    if definition.workspace_scoped:
        assert isinstance(context, ScopeContext)
        values["workspace_id"] = context.workspace_id
        values["workspace_scope"] = f"value:{context.workspace_id}"
    return values


def seed_sqlalchemy_mapping(
    engine: Engine,
    model: type[Any],
    *,
    tenant_id: str,
    token: str,
    overrides: Mapping[str, Any] | None = None,
    seeded: dict[tuple[type[Any], str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seed a mapped row from mapper metadata, recursively creating FK parents.

    Overrides are deliberately a validation escape hatch; ordinary identity,
    type, default, nullability and foreign-key values come from the mapping.
    """
    cache = {} if seeded is None else seeded
    key = (model, tenant_id, token)
    if key in cache:
        return cache[key]
    mapper = sa_inspect(model)
    definition: ResourceScopeDefinition = model.scope_definition
    values: dict[str, Any] = {}
    cache[key] = values
    table_to_model = {
        candidate.local_table.name: candidate.class_ for candidate in mapper.registry.mappers
    }
    for constraint in mapper.local_table.foreign_key_constraints:
        parent_model = table_to_model.get(constraint.referred_table.name)
        if parent_model is None:
            raise AssertionError(
                f"mapped foreign-key parent missing for {constraint.referred_table.name}"
            )
        parent_values = seed_sqlalchemy_mapping(
            engine,
            parent_model,
            tenant_id=tenant_id,
            token=token,
            seeded=cache,
        )
        for element in constraint.elements:
            values[element.parent.name] = parent_values[element.column.name]
    for column in mapper.columns:
        if definition.scope is ResourceScope.TENANT_SCOPED and column.name == "tenant_id":
            continue
        if definition.workspace_scoped and column.name == "workspace_id":
            continue
        if column.name in values or column.default is not None or column.server_default is not None:
            continue
        if column.nullable and not column.primary_key:
            continue
        if column.autoincrement is True or (
            column.autoincrement == "auto" and column.primary_key and _is_integer_column(column)
        ):
            continue
        values[column.name] = _mapped_column_value(column, token)
    if overrides:
        unknown = set(overrides) - {column.name for column in mapper.columns}
        if unknown:
            raise ValueError(f"unknown mapped seed overrides: {sorted(unknown)}")
        values.update(overrides)
    instance = model(
        **{
            mapper.get_property_by_column(mapper.local_table.c[name]).key: value
            for name, value in values.items()
        }
    )
    with Session(engine) as raw:
        if definition.scope is ResourceScope.TENANT_SCOPED:
            scoped = ScopedSession(raw, _sqlalchemy_context(definition, tenant_id))
            scoped.add(instance)
            scoped.commit()
            scoped.refresh(instance)
        else:
            raw.add(instance)
            raw.commit()
            raw.refresh(instance)
        for column in mapper.columns:
            values[column.name] = getattr(instance, mapper.get_property_by_column(column).key)
    return values


def exercise_sqlalchemy_case(engine: Engine, case: CrossTenantCase) -> None:
    """Exercise one generated SQLAlchemy mapping operation through ScopedSession."""
    model = case.mapped_model
    if model is None:
        raise TypeError("SQLAlchemy cases require a mapped model")
    definition = case.definition
    owner_values = seed_sqlalchemy_mapping(engine, model, tenant_id="matrix-owner", token="owner")
    mapper = sa_inspect(model)
    identity = tuple(owner_values[column.name] for column in mapper.primary_key)
    identity_arg: Any = identity[0] if len(identity) == 1 else identity
    owner_scope = _sqlalchemy_context(definition, "matrix-owner")
    foreign_scope = _sqlalchemy_context(definition, "matrix-foreign")
    update_column: Any | None = None
    original_update_value: Any = None
    with Session(engine) as raw:
        foreign = ScopedSession(raw, foreign_scope)
        if case.operation is ResourceOperation.READ:
            statement = select(model).where(
                *(
                    getattr(model, column.key) == value
                    for column, value in zip(mapper.primary_key, identity, strict=True)
                )
            )
            assert foreign.scalars(statement).one_or_none() is None
        elif case.operation is ResourceOperation.ENUMERATE:
            assert foreign.scalars(select(model)).all() == []
        elif case.operation is ResourceOperation.UPDATE:
            update_column = next(
                (
                    item
                    for item in mapper.columns
                    if not item.primary_key and item.name not in {"tenant_id", "workspace_id"}
                ),
                None,
            )
            if update_column is None:
                update_column = next(
                    item
                    for item in mapper.primary_key
                    if item.name not in {"tenant_id", "workspace_id"}
                )
            original_update_value = owner_values[update_column.name]
            foreign.execute(
                update(model).values(
                    **{
                        update_column.key: _different_mapped_value(
                            update_column, original_update_value
                        )
                    }
                )
            )
            foreign.commit()
        elif case.operation is ResourceOperation.DELETE:
            foreign.execute(delete(model))
            foreign.commit()
        else:
            # Reuse the logical identifier when the physical identity (including
            # every recursively seeded parent) is actually scope-partitioned.
            # Legacy mapped tables with a globally unique primary key cannot
            # represent the same identifier twice, so they use a distinct value
            # while still proving bound ownership injection and retrieval.
            foreign_token = "owner" if _scope_partitioned_identity(model) else "foreign"
            foreign_values = seed_sqlalchemy_mapping(
                engine, model, tenant_id="matrix-foreign", token=foreign_token
            )
            foreign_identity = tuple(foreign_values[column.name] for column in mapper.primary_key)
            foreign_identity_arg: Any = (
                foreign_identity[0] if len(foreign_identity) == 1 else foreign_identity
            )
            assert foreign.get(model, foreign_identity_arg) is not None
    with Session(engine) as raw:
        owner = ScopedSession(raw, owner_scope)
        owner_row = owner.get(model, identity_arg)
        assert owner_row is not None
        if update_column is not None:
            assert getattr(owner_row, update_column.key) == original_update_value


def _sqlalchemy_context(definition: ResourceScopeDefinition, tenant_id: str) -> ScopeContext:
    # The ORM gateway accepts the full trusted context; non-workspace mappings
    # simply ignore its workspace component.
    return ScopeContext(tenant_id, "matrix-workspace")


def _scope_partitioned_identity(model: type[Any], seen: frozenset[type[Any]] = frozenset()) -> bool:
    if model in seen:
        return True
    mapper = sa_inspect(model)
    primary_key_names = {column.name for column in mapper.primary_key}
    if "tenant_id" not in primary_key_names:
        return False
    definition: ResourceScopeDefinition = model.scope_definition
    if definition.workspace_scoped and "workspace_id" not in primary_key_names:
        return False
    table_to_model = {
        candidate.local_table.name: candidate.class_ for candidate in mapper.registry.mappers
    }
    parents = (
        table_to_model.get(constraint.referred_table.name)
        for constraint in mapper.local_table.foreign_key_constraints
    )
    return all(
        parent is not None and _scope_partitioned_identity(parent, seen | {model})
        for parent in parents
    )


def _is_integer_column(column: Any) -> bool:
    try:
        return column.type.python_type is int
    except (AttributeError, NotImplementedError):
        return False


def _mapped_column_value(column: Any, token: str) -> Any:
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        python_type = str
    if python_type is int:
        return 1
    if python_type is float:
        return 1.0
    if python_type is bool:
        return False
    if python_type is dict:
        return {}
    if python_type is list:
        return []
    from datetime import UTC, datetime
    from decimal import Decimal

    if python_type is datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)
    if python_type is Decimal:
        return Decimal("1")
    return f"matrix-{token}-{column.name}"


def _different_mapped_value(column: Any, original: Any) -> Any:
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    if isinstance(original, bool):
        return not original
    if isinstance(original, (datetime, date)):
        return original + timedelta(days=1)
    if isinstance(original, Decimal):
        return original + Decimal("1")
    if isinstance(original, (int, float)):
        return original + 1
    if isinstance(original, dict):
        return {"changed": True}
    if isinstance(original, list):
        return ["changed"]
    return f"{original}-changed"


def _column_value(column: ReflectedColumn, token: str) -> Any:
    name = column.name
    kind = column.sql_type.upper()
    if "INT" in kind:
        return 1
    if any(item in kind for item in ("REAL", "FLOAT", "DOUBLE", "NUMERIC")):
        return 1.0
    if name == "reason":
        return "rte"
    if name in {"status", "state"}:
        return "pending"
    if name == "target_url":
        return f"https://{token}.example.test/hook"
    if name.endswith("_json") or name in {
        "payload",
        "record_json",
        "metadata",
        "artifacts",
        "channels",
    }:
        return "{}"
    if name.endswith("s") or name in {
        "completed_steps",
        "current_node_ids",
        "event_types",
    }:
        return "[]"
    if name == "schema_version":
        return 1
    return f"matrix-{token}-{name}"


def _different_value(column: ReflectedColumn, original: Any) -> Any:
    if isinstance(original, int):
        return original + 1
    if isinstance(original, float):
        return original + 1.0
    if column.name in {"status", "state"}:
        return "completed"
    return f"{original}-changed"
