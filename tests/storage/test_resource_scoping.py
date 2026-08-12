from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from zeroth.platform.storage import (
    GlobalTable,
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopedJoin,
    ScopedTable,
    ScopeContext,
    TenantWideScopeContext,
)
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append(("execute", sql, params))

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        self.calls.append(("fetch_one", sql, params))
        return {"run_id": "run-1"}

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self.calls.append(("fetch_all", sql, params))
        return [{"run_id": "run-1"}]

    async def execute_script(self, sql: str) -> None:
        raise AssertionError("structured tables never execute raw scripts")


class _RecordingDatabase:
    backend = "sqlite"

    def __init__(self) -> None:
        self.connection = _RecordingConnection()
        self.transactions: list[bool] = []

    @asynccontextmanager
    async def transaction(self, *, write_lock: bool = False) -> AsyncIterator[_RecordingConnection]:
        self.transactions.append(write_lock)
        yield self.connection

    async def close(self) -> None:
        pass


def _gateway_registry() -> ResourceScopeRegistry:
    return ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runs",
                workspace_scoped=True,
                operations=frozenset(ResourceOperation),
            ),
            ResourceScopeDefinition(
                resource_name="checkpoints",
                table_name="run_checkpoints",
                workspace_scoped=True,
                operations=frozenset(ResourceOperation),
            ),
            ResourceScopeDefinition(
                resource_name="schema-versions",
                table_name="schema_versions",
                scope=ResourceScope.GLOBAL,
                operations=frozenset(ResourceOperation),
            ),
        ]
    )


def _scoped_table(database: _RecordingDatabase) -> ScopedTable:
    return ScopedTable(
        database,
        _gateway_registry(),
        "runs",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )


def test_resource_scope_has_only_tenant_scoped_and_global_values() -> None:
    assert set(ResourceScope) == {ResourceScope.TENANT_SCOPED, ResourceScope.GLOBAL}
    assert ResourceScope.TENANT_SCOPED.value == "tenant_scoped"
    assert ResourceScope.GLOBAL.value == "global"


def test_resource_operation_has_explicit_persistent_operations() -> None:
    assert set(ResourceOperation) == {
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.ENUMERATE,
        ResourceOperation.UPDATE,
        ResourceOperation.DELETE,
    }


@pytest.mark.parametrize("tenant_id", ["", "  ", None, 7])
def test_scope_context_rejects_invalid_tenant_id(tenant_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ScopeContext(tenant_id=tenant_id, workspace_id="workspace-a")  # type: ignore[arg-type]


@pytest.mark.parametrize("workspace_id", ["", "  ", None, 7])
def test_scope_context_requires_a_non_empty_workspace_id(workspace_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ScopeContext(tenant_id="tenant-a", workspace_id=workspace_id)  # type: ignore[arg-type]


def test_scope_context_has_no_implicit_default_and_is_immutable() -> None:
    with pytest.raises(TypeError):
        ScopeContext()  # type: ignore[call-arg]

    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")

    with pytest.raises(FrozenInstanceError):
        context.tenant_id = "tenant-b"  # type: ignore[misc]


def test_tenant_wide_scope_is_a_distinct_immutable_privileged_context() -> None:
    context = TenantWideScopeContext(tenant_id="tenant-a")

    assert not isinstance(context, ScopeContext)
    assert not hasattr(context, "workspace_id")
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = "tenant-b"  # type: ignore[misc]


@pytest.mark.parametrize("tenant_id", ["", "  ", None, 7])
def test_tenant_wide_scope_rejects_invalid_tenant_id(tenant_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        TenantWideScopeContext(tenant_id=tenant_id)  # type: ignore[arg-type]


def test_default_compatibility_contexts_require_an_explicit_constructor() -> None:
    with pytest.raises(ValueError, match="compatibility"):
        ScopeContext(tenant_id="default", workspace_id="workspace-a")
    with pytest.raises(ValueError, match="compatibility"):
        TenantWideScopeContext(tenant_id="default")

    workspace_context = ScopeContext.for_default_compatibility(workspace_id="workspace-a")
    tenant_context = TenantWideScopeContext.for_default_compatibility()

    assert workspace_context.tenant_id == "default"
    assert workspace_context.workspace_id == "workspace-a"
    assert tenant_context.tenant_id == "default"


def test_resource_definition_defaults_operational_data_to_tenant_scope() -> None:
    definition = ResourceScopeDefinition(
        resource_name="runs",
        table_name="runtime_runs",
        operations=frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
    )

    assert definition.scope is ResourceScope.TENANT_SCOPED
    assert definition.workspace_scoped is False
    assert definition.direct_scope_ready is True


@pytest.mark.parametrize(
    "table_name",
    ["runs; DROP TABLE runs", "runs -- comment", 'runs"quoted', "schema.runs"],
)
def test_resource_definition_rejects_non_identifier_table_names(table_name: str) -> None:
    with pytest.raises(ValueError, match="table_name"):
        ResourceScopeDefinition(
            resource_name="runs",
            table_name=table_name,
            operations=frozenset({ResourceOperation.READ}),
        )


@pytest.mark.parametrize("resource_name", ["runs;drop", "runs comment", "runs/other"])
def test_resource_definition_rejects_unstable_resource_names(resource_name: str) -> None:
    with pytest.raises(ValueError, match="resource_name"):
        ResourceScopeDefinition(
            resource_name=resource_name,
            table_name="runs",
            operations=frozenset({ResourceOperation.READ}),
        )


def test_resource_definition_is_immutable_and_requires_stable_names_and_operations() -> None:
    definition = ResourceScopeDefinition(
        resource_name="runs",
        table_name="runtime_runs",
        operations=frozenset({ResourceOperation.READ}),
    )

    with pytest.raises(FrozenInstanceError):
        definition.table_name = "other"  # type: ignore[misc]

    for kwargs in (
        {"resource_name": "", "table_name": "runtime_runs"},
        {"resource_name": "runs", "table_name": "  "},
    ):
        with pytest.raises(ValueError):
            ResourceScopeDefinition(
                **kwargs,
                operations=frozenset({ResourceOperation.READ}),
            )

    with pytest.raises(ValueError, match="operations"):
        ResourceScopeDefinition(
            resource_name="runs",
            table_name="runtime_runs",
            operations=frozenset(),
        )


def test_workspace_scoped_definition_must_be_tenant_scoped() -> None:
    with pytest.raises(ValueError, match="workspace_scoped"):
        ResourceScopeDefinition(
            resource_name="global-settings",
            table_name="global_settings",
            scope=ResourceScope.GLOBAL,
            workspace_scoped=True,
            operations=frozenset({ResourceOperation.READ}),
        )


def test_registry_rejects_duplicate_resource_and_table_names() -> None:
    registry = ResourceScopeRegistry()
    registry.register(
        ResourceScopeDefinition(
            resource_name="runs",
            table_name="runtime_runs",
            operations=frozenset({ResourceOperation.READ}),
        )
    )

    with pytest.raises(ValueError, match="resource_name"):
        registry.register(
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="other_runs",
                operations=frozenset({ResourceOperation.READ}),
            )
        )
    with pytest.raises(ValueError, match="table_name"):
        registry.register(
            ResourceScopeDefinition(
                resource_name="archived-runs",
                table_name="runtime_runs",
                operations=frozenset({ResourceOperation.READ}),
            )
        )


def test_registry_resolves_definitions_by_resource_and_physical_table() -> None:
    definition = ResourceScopeDefinition(
        resource_name="runs",
        table_name="runtime_runs",
        operations=frozenset({ResourceOperation.READ}),
    )
    registry = ResourceScopeRegistry([definition])

    by_resource = registry.definition_for_resource("runs")
    by_table = registry.definition_for_table("runtime_runs")

    assert by_resource == definition
    assert by_table == definition
    assert by_resource is not definition
    assert by_table is not definition
    assert by_resource is not by_table
    with pytest.raises(KeyError):
        registry.definition_for_resource("missing")


def test_registry_snapshots_caller_definition_values_at_registration() -> None:
    definition = ResourceScopeDefinition(
        resource_name="children",
        table_name="children",
        workspace_scoped=True,
        operations=frozenset({ResourceOperation.CREATE}),
    )
    expected = ResourceScopeDefinition(
        resource_name="children",
        table_name="children",
        workspace_scoped=True,
        operations=frozenset({ResourceOperation.CREATE}),
    )
    registry = ResourceScopeRegistry([definition])

    object.__setattr__(definition, "resource_name", "parents")
    object.__setattr__(definition, "table_name", "forged_children")
    object.__setattr__(definition, "workspace_scoped", False)
    object.__setattr__(definition, "operations", frozenset({ResourceOperation.ENUMERATE}))
    object.__setattr__(definition, "direct_scope_ready", False)

    assert registry.definition_for_resource("children") == expected
    assert registry.definition_for_table("children") == expected
    with pytest.raises(KeyError):
        registry.definition_for_resource("parents")
    with pytest.raises(KeyError):
        registry.definition_for_table("forged_children")


def test_registry_lookup_and_definition_views_are_defensive_copies() -> None:
    expected = ResourceScopeDefinition(
        resource_name="children",
        table_name="children",
        workspace_scoped=True,
        operations=frozenset({ResourceOperation.CREATE}),
    )
    registry = ResourceScopeRegistry([expected])
    lookup = registry.definition_for_resource("children")
    view = registry.definitions[0]

    for exposed in (lookup, view):
        object.__setattr__(exposed, "resource_name", "parents")
        object.__setattr__(exposed, "table_name", "forged_children")
        object.__setattr__(exposed, "workspace_scoped", False)
        object.__setattr__(
            exposed,
            "operations",
            frozenset({ResourceOperation.ENUMERATE}),
        )

    fresh = registry.definition_for_resource("children")
    assert fresh == expected
    assert fresh is not expected
    assert registry.definitions == (expected,)
    with pytest.raises(ValueError, match="operation"):
        registry.validate_binding(
            "children",
            ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
            operation=ResourceOperation.ENUMERATE,
        )
    with pytest.raises(ValueError, match="workspace"):
        registry.validate_binding(
            "children",
            TenantWideScopeContext(tenant_id="tenant-a"),
            operation=ResourceOperation.CREATE,
        )


def test_registry_exposes_an_immutable_definition_snapshot() -> None:
    first = ResourceScopeDefinition(
        resource_name="runs",
        table_name="runtime_runs",
        operations=frozenset({ResourceOperation.READ}),
    )
    second = ResourceScopeDefinition(
        resource_name="audit-events",
        table_name="audit_events",
        operations=frozenset({ResourceOperation.CREATE}),
    )
    registry = ResourceScopeRegistry([first])

    snapshot = registry.definitions
    registry.register(second)

    assert snapshot == (first,)
    assert registry.definitions == (first, second)


def test_global_definition_accepts_only_an_unscoped_binding() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="schema-migrations",
                table_name="schema_migrations",
                scope=ResourceScope.GLOBAL,
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )

    registry.validate_binding("schema-migrations", None)
    with pytest.raises(ValueError, match="global"):
        registry.validate_binding(
            "schema-migrations",
            ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
        )
    with pytest.raises(ValueError, match="global"):
        registry.validate_binding("schema-migrations", TenantWideScopeContext(tenant_id="tenant-a"))


def test_tenant_definition_requires_a_tenant_context() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="audit-events",
                table_name="audit_events",
                operations=frozenset({ResourceOperation.CREATE}),
            )
        ]
    )

    with pytest.raises(ValueError, match="tenant"):
        registry.validate_binding("audit-events", None)
    registry.validate_binding(
        "audit-events", ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    registry.validate_binding("audit-events", TenantWideScopeContext(tenant_id="tenant-a"))


def test_workspace_definition_rejects_tenant_wide_binding_by_default() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runtime_runs",
                workspace_scoped=True,
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )

    registry.validate_binding(
        "runs", ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    with pytest.raises(ValueError, match="workspace"):
        registry.validate_binding("runs", TenantWideScopeContext(tenant_id="tenant-a"))

    registry.validate_privileged_tenant_wide_binding(
        "runs", TenantWideScopeContext(tenant_id="tenant-a")
    )


def test_privileged_tenant_wide_binding_still_enforces_resource_contracts() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runtime_runs",
                workspace_scoped=True,
                operations=frozenset({ResourceOperation.READ}),
            ),
            ResourceScopeDefinition(
                resource_name="schema-migrations",
                table_name="schema_migrations",
                scope=ResourceScope.GLOBAL,
                operations=frozenset({ResourceOperation.READ}),
            ),
        ]
    )
    tenant_wide = TenantWideScopeContext(tenant_id="tenant-a")

    with pytest.raises(TypeError, match="TenantWideScopeContext"):
        registry.validate_privileged_tenant_wide_binding(
            "runs",
            ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="global"):
        registry.validate_privileged_tenant_wide_binding("schema-migrations", tenant_wide)
    with pytest.raises(ValueError, match="operation"):
        registry.validate_privileged_tenant_wide_binding(
            "runs", tenant_wide, operation=ResourceOperation.DELETE
        )


def test_registry_rejects_an_operation_not_declared_by_the_resource() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runtime_runs",
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )

    registry.validate_binding(
        "runs",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
        operation=ResourceOperation.READ,
    )
    with pytest.raises(ValueError, match="operation"):
        registry.validate_binding(
            "runs",
            ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
            operation=ResourceOperation.DELETE,
        )


def test_registry_rejects_a_definition_subclass_that_bypasses_validation() -> None:
    class ForgedDefinition(ResourceScopeDefinition):
        def __post_init__(self) -> None:
            pass

    forged = ForgedDefinition(
        resource_name="",
        table_name="runtime_runs",
        operations=frozenset(),
    )

    with pytest.raises(TypeError, match="ResourceScopeDefinition"):
        ResourceScopeRegistry([forged])


def test_binding_rejects_a_scope_context_subclass_that_bypasses_validation() -> None:
    class ForgedScopeContext(ScopeContext):
        def __post_init__(self) -> None:
            pass

    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runtime_runs",
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )
    forged = ForgedScopeContext(tenant_id="default", workspace_id="")

    with pytest.raises(TypeError, match="recognized scope context"):
        registry.validate_binding("runs", forged)


def test_privileged_binding_rejects_a_tenant_wide_subclass() -> None:
    class ForgedTenantWideScopeContext(TenantWideScopeContext):
        def __post_init__(self) -> None:
            pass

    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runtime_runs",
                workspace_scoped=True,
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )
    forged = ForgedTenantWideScopeContext(tenant_id="default")

    with pytest.raises(TypeError, match="TenantWideScopeContext"):
        registry.validate_privileged_tenant_wide_binding("runs", forged)


@pytest.mark.asyncio
async def test_scoped_table_adds_tenant_and_workspace_to_every_crud_operation() -> None:
    database = _RecordingDatabase()
    table = _scoped_table(database)

    await table.select(where={"run_id": "run-1"})
    await table.insert({"run_id": "run-1", "status": "pending"})
    await table.update({"status": "done"}, where={"run_id": "run-1"})
    await table.delete(where={"run_id": "run-1"})

    calls = database.connection.calls
    assert [call[0] for call in calls] == ["fetch_all", "execute", "execute", "execute"]
    assert "tenant_id = ?" in calls[0][1] and "workspace_id = ?" in calls[0][1]
    assert calls[0][2] == ("run-1", "tenant-a", "workspace-a", "value:workspace-a")
    assert "tenant_id" in calls[1][1] and "workspace_id" in calls[1][1]
    assert calls[1][2] == (
        "run-1",
        "pending",
        "tenant-a",
        "workspace-a",
        "value:workspace-a",
    )
    for _, sql, params in calls[2:]:
        assert "tenant_id = ?" in sql and "workspace_id = ?" in sql
        assert params[-3:] == ("tenant-a", "workspace-a", "value:workspace-a")
    assert database.transactions == [False, True, True, True]


@pytest.mark.asyncio
async def test_scoped_table_rejects_inserted_or_updated_ownership() -> None:
    table = _scoped_table(_RecordingDatabase())

    with pytest.raises(ValueError, match="tenant_id"):
        await table.insert({"run_id": "run-1", "tenant_id": "tenant-b"})
    with pytest.raises(ValueError, match="workspace_id"):
        await table.insert({"run_id": "run-1", "workspace_id": "workspace-b"})
    with pytest.raises(ValueError, match="ownership"):
        await table.update({"tenant_id": "tenant-a"}, where={"run_id": "run-1"})


@pytest.mark.asyncio
async def test_foreign_key_join_scopes_both_tenant_tables() -> None:
    database = _RecordingDatabase()
    registry = _gateway_registry()
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    runs = ScopedTable(database, registry, "runs", context)
    checkpoints = ScopedTable(database, registry, "checkpoints", context)

    await runs.select(
        where={"run_id": "run-1"},
        joins=(
            ScopedJoin(
                table=checkpoints,
                local_column="run_id",
                foreign_column="run_id",
            ),
        ),
    )

    _, sql, params = database.connection.calls[0]
    assert "JOIN run_checkpoints AS j1 ON runs.run_id = j1.run_id" in sql
    assert "runs.tenant_id = ?" in sql and "runs.workspace_id = ?" in sql
    assert "j1.tenant_id = ?" in sql and "j1.workspace_id = ?" in sql
    assert params == (
        "run-1",
        "tenant-a",
        "workspace-a",
        "value:workspace-a",
        "tenant-a",
        "workspace-a",
        "value:workspace-a",
    )


@pytest.mark.asyncio
async def test_scoped_table_rejects_foreign_scope_join() -> None:
    database = _RecordingDatabase()
    registry = _gateway_registry()
    runs = ScopedTable(
        database,
        registry,
        "runs",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    foreign = ScopedTable(
        database,
        registry,
        "checkpoints",
        ScopeContext(tenant_id="tenant-b", workspace_id="workspace-a"),
    )

    with pytest.raises(ValueError, match="same scope"):
        await runs.select(
            joins=(ScopedJoin(table=foreign, local_column="run_id", foreign_column="run_id"),)
        )


@pytest.mark.asyncio
async def test_scoped_table_rejects_create_only_join_before_query() -> None:
    database = _RecordingDatabase()
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runs",
                operations=frozenset({ResourceOperation.ENUMERATE}),
            ),
            ResourceScopeDefinition(
                resource_name="children",
                table_name="children",
                operations=frozenset({ResourceOperation.CREATE}),
            ),
        ]
    )
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")

    with pytest.raises(ValueError, match="operation"):
        await ScopedTable(database, registry, "runs", context).select(
            joins=(
                ScopedJoin(
                    table=ScopedTable(database, registry, "children", context),
                    local_column="run_id",
                    foreign_column="run_id",
                ),
            )
        )

    assert database.transactions == []


@pytest.mark.asyncio
async def test_scoped_join_cannot_replace_child_definition_to_bypass_operations() -> None:
    database = _RecordingDatabase()
    child_definition = ResourceScopeDefinition(
        resource_name="children",
        table_name="children",
        workspace_scoped=True,
        operations=frozenset({ResourceOperation.CREATE}),
    )
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="parents",
                table_name="parents",
                workspace_scoped=True,
                operations=frozenset({ResourceOperation.ENUMERATE}),
            ),
            child_definition,
        ]
    )
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    parent = ScopedTable(database, registry, "parents", context)
    child = ScopedTable(database, registry, "children", context)
    forged = ResourceScopeDefinition(
        resource_name="parents",
        table_name="children",
        workspace_scoped=False,
        operations=frozenset({ResourceOperation.ENUMERATE}),
    )

    with pytest.raises(AttributeError):
        child._definition = forged  # type: ignore[misc]

    exposed_definitions = (
        child_definition,
        registry.definition_for_resource("children"),
        next(
            definition
            for definition in registry.definitions
            if definition.resource_name == "children"
        ),
    )
    for exposed in exposed_definitions:
        object.__setattr__(exposed, "resource_name", "parents")
        object.__setattr__(exposed, "table_name", "children")
        object.__setattr__(exposed, "workspace_scoped", False)
        object.__setattr__(
            exposed,
            "operations",
            frozenset({ResourceOperation.ENUMERATE}),
        )
    with pytest.raises(ValueError, match="operation"):
        await parent.select(
            joins=(
                ScopedJoin(
                    table=child,
                    local_column="parent_id",
                    foreign_column="parent_id",
                ),
            )
        )

    assert child._definition == registry.definition_for_resource("children")
    assert child._definition is not registry.definition_for_resource("children")
    assert database.transactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["select", "insert", "update", "delete"])
async def test_scoped_crud_cannot_replace_canonical_definition(operation: str) -> None:
    database = _RecordingDatabase()
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="restricted",
                table_name="restricted",
                workspace_scoped=True,
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )
    table = ScopedTable(
        database,
        registry,
        "restricted",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    forged = ResourceScopeDefinition(
        resource_name="restricted",
        table_name="forged",
        workspace_scoped=False,
        operations=frozenset(ResourceOperation),
    )

    with pytest.raises(AttributeError):
        table._definition = forged  # type: ignore[misc]
    with pytest.raises(ValueError, match="operation"):
        if operation == "select":
            await table.select()
        elif operation == "insert":
            await table.insert({"id": "row-1"})
        elif operation == "update":
            await table.update({"value": "changed"}, where={"id": "row-1"})
        else:
            await table.delete(where={"id": "row-1"})

    assert database.transactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["select", "insert", "update", "delete"])
async def test_global_crud_cannot_replace_canonical_definition(operation: str) -> None:
    database = _RecordingDatabase()
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="reference-data",
                table_name="reference_data",
                scope=ResourceScope.GLOBAL,
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )
    table = GlobalTable(database, registry, "reference-data")
    forged = ResourceScopeDefinition(
        resource_name="reference-data",
        table_name="forged_reference_data",
        scope=ResourceScope.GLOBAL,
        operations=frozenset(ResourceOperation),
    )

    with pytest.raises(AttributeError):
        table._definition = forged  # type: ignore[misc]
    with pytest.raises(ValueError, match="operation"):
        if operation == "select":
            await table.select()
        elif operation == "insert":
            await table.insert({"id": "row-1"})
        elif operation == "update":
            await table.update({"value": "changed"}, where={"id": "row-1"})
        else:
            await table.delete(where={"id": "row-1"})

    assert database.transactions == []


def test_scoped_table_internal_binding_fields_are_read_only() -> None:
    database = _RecordingDatabase()
    registry = _gateway_registry()
    table = ScopedTable(
        database,
        registry,
        "runs",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )

    with pytest.raises(AttributeError):
        table._registry = ResourceScopeRegistry()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        table._context = TenantWideScopeContext(tenant_id="tenant-a")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        table._privileged_tenant_wide = True  # type: ignore[misc]


@pytest.mark.asyncio
async def test_select_rejects_forged_join_before_real_sqlite_query(tmp_path) -> None:
    class CountingSQLiteDatabase(AsyncSQLiteDatabase):
        def __init__(self, path: str) -> None:
            super().__init__(path)
            self.transaction_count = 0

        @asynccontextmanager
        async def transaction(self, *, write_lock: bool = False):
            self.transaction_count += 1
            async with super().transaction(write_lock=write_lock) as connection:
                yield connection

    class ForgedJoin(ScopedJoin):
        def __post_init__(self) -> None:
            pass

    database = CountingSQLiteDatabase(str(tmp_path / "join-injection.db"))
    async with database.transaction() as connection:
        await connection.execute_script(
            """
            CREATE TABLE runs (
                run_id TEXT, tenant_id TEXT, workspace_id TEXT
            );
            CREATE TABLE children (
                run_id TEXT, tenant_id TEXT, workspace_id TEXT
            );
            INSERT INTO runs VALUES ('run-a', 'tenant-a', 'workspace-a');
            INSERT INTO runs VALUES ('run-b', 'tenant-b', 'workspace-b');
            INSERT INTO children VALUES ('run-a', 'tenant-a', 'workspace-a');
            INSERT INTO children VALUES ('run-b', 'tenant-b', 'workspace-b');
            """
        )
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="runs",
                table_name="runs",
                operations=frozenset({ResourceOperation.ENUMERATE}),
            ),
            ResourceScopeDefinition(
                resource_name="children",
                table_name="children",
                operations=frozenset({ResourceOperation.ENUMERATE}),
            ),
        ]
    )
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    runs = ScopedTable(database, registry, "runs", context)
    children = ScopedTable(database, registry, "children", context)
    forged = ForgedJoin(
        table=children,
        local_column="run_id = j1.run_id OR 1 = 1 --",
        foreign_column="run_id",
    )
    transactions_before_attack = database.transaction_count

    with pytest.raises(TypeError, match="ScopedJoin"):
        await runs.select(joins=(forged,))

    assert database.transaction_count == transactions_before_attack
    async with database.transaction() as connection:
        rows = await connection.fetch_all("SELECT tenant_id FROM runs ORDER BY tenant_id")
    assert rows == [{"tenant_id": "tenant-a"}, {"tenant_id": "tenant-b"}]


@pytest.mark.asyncio
async def test_select_revalidates_exact_join_identifiers_at_render_time() -> None:
    database = _RecordingDatabase()
    registry = _gateway_registry()
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    join = ScopedJoin(
        table=ScopedTable(database, registry, "checkpoints", context),
        local_column="run_id",
        foreign_column="run_id",
    )
    object.__setattr__(join, "local_column", "run_id OR 1 = 1 --")

    with pytest.raises(ValueError, match="identifier"):
        await ScopedTable(database, registry, "runs", context).select(joins=(join,))

    assert database.transactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["select", "select_one", "insert", "update", "delete"],
)
async def test_gateway_ignores_mutation_of_exposed_definition_copy(operation: str) -> None:
    database = _RecordingDatabase()
    table = _scoped_table(database)
    exposed = table._definition
    object.__setattr__(exposed, "table_name", "runs; DROP TABLE runs")
    object.__setattr__(exposed, "workspace_scoped", False)

    if operation == "select":
        await table.select()
    elif operation == "select_one":
        await table.select_one(where={"run_id": "run-1"})
    elif operation == "insert":
        await table.insert({"run_id": "run-1"})
    elif operation == "update":
        await table.update({"status": "done"}, where={"run_id": "run-1"})
    else:
        await table.delete(where={"run_id": "run-1"})

    _, sql, params = database.connection.calls[0]
    assert "runs; DROP TABLE runs" not in sql
    assert "runs" in sql
    assert "workspace_id = ?" in sql or "workspace_id" in sql
    assert "workspace-a" in params


def test_scoped_and_global_gateways_are_separate_and_hide_raw_database() -> None:
    database = _RecordingDatabase()
    registry = _gateway_registry()
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")

    with pytest.raises(ValueError, match="global"):
        ScopedTable(database, registry, "schema-versions", context)
    with pytest.raises(ValueError, match="tenant"):
        GlobalTable(database, registry, "runs")

    scoped = ScopedTable(database, registry, "runs", context)
    global_table = GlobalTable(database, registry, "schema-versions")
    for gateway in (scoped, global_table):
        assert not hasattr(gateway, "database")
        assert not hasattr(gateway, "connection")
        assert not hasattr(gateway, "execute")

    assert callable(scoped.transaction)
    assert callable(global_table.transaction)


@pytest.mark.asyncio
async def test_global_table_never_adds_tenant_predicates_or_accepts_tenant_columns() -> None:
    database = _RecordingDatabase()
    table = GlobalTable(database, _gateway_registry(), "schema-versions")

    await table.select(where={"scope": "service"})
    await table.insert({"scope": "service", "version": 1})
    with pytest.raises(ValueError, match="tenant"):
        await table.insert({"scope": "service", "tenant_id": "tenant-a"})
    with pytest.raises(ValueError, match="tenant"):
        await table.select(where={"tenant_id": "tenant-a"})

    assert all(
        "tenant_id" not in sql and "workspace_id" not in sql
        for _, sql, _ in database.connection.calls
    )


@pytest.mark.asyncio
async def test_privileged_tenant_wide_gateway_is_explicit_and_omits_workspace() -> None:
    database = _RecordingDatabase()
    context = TenantWideScopeContext(tenant_id="tenant-a")

    with pytest.raises(ValueError, match="workspace"):
        ScopedTable(database, _gateway_registry(), "runs", context)

    table = ScopedTable.for_privileged_tenant_wide(database, _gateway_registry(), "runs", context)
    await table.select(where={"run_id": "run-1"})

    _, sql, params = database.connection.calls[0]
    assert "tenant_id = ?" in sql
    assert "workspace_id" not in sql
    assert params == ("run-1", "tenant-a")

    with pytest.raises(ValueError, match="workspace"):
        await table.insert({"run_id": "run-2"})


@pytest.mark.asyncio
async def test_structured_mutations_require_a_caller_predicate() -> None:
    database = _RecordingDatabase()
    global_table = GlobalTable(database, _gateway_registry(), "schema-versions")

    with pytest.raises(ValueError, match="where"):
        await global_table.update({"version": 2}, where={})
    with pytest.raises(ValueError, match="where"):
        await global_table.delete(where={})
