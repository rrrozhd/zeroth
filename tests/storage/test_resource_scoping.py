from dataclasses import FrozenInstanceError

import pytest

from zeroth.platform.storage import (
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
    TenantWideScopeContext,
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

    assert registry.definition_for_resource("runs") is definition
    assert registry.definition_for_table("runtime_runs") is definition
    with pytest.raises(KeyError):
        registry.definition_for_resource("missing")


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
        registry.validate_binding(
            "schema-migrations", TenantWideScopeContext(tenant_id="tenant-a")
        )


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
