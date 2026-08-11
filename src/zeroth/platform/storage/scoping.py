"""Authoritative resource scope definitions for persistent storage."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import final

_DEFAULT_TENANT_ID = "default"
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ResourceScope(StrEnum):
    """Ownership boundary applied to a persistent resource."""

    TENANT_SCOPED = "tenant_scoped"
    GLOBAL = "global"


class ResourceOperation(StrEnum):
    """Persistent operations a resource may expose."""

    CREATE = "create"
    READ = "read"
    ENUMERATE = "enumerate"
    UPDATE = "update"
    DELETE = "delete"


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_stable_resource_name(value: object) -> None:
    _require_non_empty_string(value, "resource_name")
    if _RESOURCE_NAME.fullmatch(value) is None:  # type: ignore[arg-type]
        raise ValueError("resource_name must be a stable identifier")


def _require_sql_identifier(value: object, field_name: str) -> None:
    _require_non_empty_string(value, field_name)
    if _SQL_IDENTIFIER.fullmatch(value) is None:  # type: ignore[arg-type]
        raise ValueError(f"{field_name} must be a SQL identifier")


@dataclass(frozen=True, slots=True)
@final
class ScopeContext:
    """A tenant and workspace identity for an ordinary scoped operation."""

    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.tenant_id, "tenant_id")
        _require_non_empty_string(self.workspace_id, "workspace_id")
        if self.tenant_id == _DEFAULT_TENANT_ID:
            raise ValueError("the reserved default tenant requires for_default_compatibility()")

    @classmethod
    def for_default_compatibility(cls, *, workspace_id: str) -> ScopeContext:
        """Build the reserved default-tenant context for migrations and tests."""
        _require_non_empty_string(workspace_id, "workspace_id")
        context = object.__new__(cls)
        object.__setattr__(context, "tenant_id", _DEFAULT_TENANT_ID)
        object.__setattr__(context, "workspace_id", workspace_id)
        return context


@dataclass(frozen=True, slots=True)
@final
class TenantWideScopeContext:
    """An explicit privileged context spanning all workspaces in one tenant."""

    tenant_id: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.tenant_id, "tenant_id")
        if self.tenant_id == _DEFAULT_TENANT_ID:
            raise ValueError("the reserved default tenant requires for_default_compatibility()")

    @classmethod
    def for_default_compatibility(cls) -> TenantWideScopeContext:
        """Build the reserved default-tenant context for migrations and tests."""
        context = object.__new__(cls)
        object.__setattr__(context, "tenant_id", _DEFAULT_TENANT_ID)
        return context


@dataclass(frozen=True, slots=True)
@final
class ResourceScopeDefinition:
    """The stable scope contract for one persistent resource."""

    resource_name: str
    table_name: str
    operations: frozenset[ResourceOperation]
    scope: ResourceScope = ResourceScope.TENANT_SCOPED
    workspace_scoped: bool = False
    direct_scope_ready: bool = True

    def __post_init__(self) -> None:
        _require_stable_resource_name(self.resource_name)
        _require_sql_identifier(self.table_name, "table_name")
        if type(self.scope) is not ResourceScope:
            raise TypeError("scope must be a ResourceScope")
        if type(self.workspace_scoped) is not bool:
            raise TypeError("workspace_scoped must be a bool")
        if type(self.direct_scope_ready) is not bool:
            raise TypeError("direct_scope_ready must be a bool")
        if not isinstance(self.operations, frozenset):
            raise TypeError("operations must be a frozenset")
        if not self.operations:
            raise ValueError("operations must be non-empty")
        if any(type(operation) is not ResourceOperation for operation in self.operations):
            raise TypeError("operations must contain only ResourceOperation members")
        if self.workspace_scoped and self.scope is ResourceScope.GLOBAL:
            raise ValueError("workspace_scoped resources must be tenant scoped")
        if not self.direct_scope_ready and self.scope is ResourceScope.GLOBAL:
            raise ValueError("global resources cannot have pending direct ownership")


type ScopeBinding = ScopeContext | TenantWideScopeContext | None


@dataclass(frozen=True, slots=True)
class _ResourceScopeSnapshot:
    """Private primitive snapshot of one validated resource definition."""

    resource_name: str
    table_name: str
    operations: frozenset[ResourceOperation]
    scope: ResourceScope
    workspace_scoped: bool
    direct_scope_ready: bool

    @classmethod
    def from_definition(cls, definition: ResourceScopeDefinition) -> _ResourceScopeSnapshot:
        validated = ResourceScopeDefinition(
            resource_name=definition.resource_name,
            table_name=definition.table_name,
            operations=definition.operations,
            scope=definition.scope,
            workspace_scoped=definition.workspace_scoped,
            direct_scope_ready=definition.direct_scope_ready,
        )
        normalized = ResourceScopeDefinition(
            resource_name=str(validated.resource_name),
            table_name=str(validated.table_name),
            operations=frozenset(validated.operations),
            scope=validated.scope,
            workspace_scoped=validated.workspace_scoped,
            direct_scope_ready=validated.direct_scope_ready,
        )
        return cls(
            resource_name=normalized.resource_name,
            table_name=normalized.table_name,
            operations=normalized.operations,
            scope=normalized.scope,
            workspace_scoped=normalized.workspace_scoped,
            direct_scope_ready=normalized.direct_scope_ready,
        )

    def to_definition(self) -> ResourceScopeDefinition:
        return ResourceScopeDefinition(
            resource_name=self.resource_name,
            table_name=self.table_name,
            operations=self.operations,
            scope=self.scope,
            workspace_scoped=self.workspace_scoped,
            direct_scope_ready=self.direct_scope_ready,
        )


class ResourceScopeRegistry:
    """Registry of stable logical resources and their physical tables."""

    __slots__ = ("__by_resource", "__by_table")

    def __init__(self, definitions: Iterable[ResourceScopeDefinition] = ()) -> None:
        self.__by_resource: dict[str, _ResourceScopeSnapshot] = {}
        self.__by_table: dict[str, _ResourceScopeSnapshot] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ResourceScopeDefinition) -> None:
        """Register a definition, rejecting either kind of duplicate identity."""
        if type(definition) is not ResourceScopeDefinition:
            raise TypeError("definition must be a ResourceScopeDefinition")
        snapshot = _ResourceScopeSnapshot.from_definition(definition)
        if snapshot.resource_name in self.__by_resource:
            raise ValueError(f"duplicate resource_name: {snapshot.resource_name!r}")
        if snapshot.table_name in self.__by_table:
            raise ValueError(f"duplicate table_name: {snapshot.table_name!r}")
        self.__by_resource[snapshot.resource_name] = snapshot
        self.__by_table[snapshot.table_name] = snapshot

    def definition_for_resource(self, resource_name: str) -> ResourceScopeDefinition:
        """Return the definition for a stable logical resource name."""
        return self.__by_resource[resource_name].to_definition()

    def definition_for_table(self, table_name: str) -> ResourceScopeDefinition:
        """Return the definition for a physical table name."""
        return self.__by_table[table_name].to_definition()

    @property
    def definitions(self) -> tuple[ResourceScopeDefinition, ...]:
        """Return an insertion-ordered immutable snapshot of all definitions."""
        return tuple(snapshot.to_definition() for snapshot in self.__by_resource.values())

    def validate_binding(
        self,
        resource_name: str,
        context: ScopeBinding,
        *,
        operation: ResourceOperation | None = None,
    ) -> ResourceScopeDefinition:
        """Validate an ordinary resource operation and return its definition."""
        definition = self.__by_resource[resource_name]
        self._validate_operation(definition, operation)
        if definition.scope is ResourceScope.GLOBAL:
            if context is not None:
                raise ValueError("global resources do not accept a tenant context")
            return definition.to_definition()
        if not definition.direct_scope_ready:
            raise ValueError(f"tenant resource {resource_name!r} has pending direct ownership")
        if context is None:
            raise ValueError("tenant-scoped resources require a tenant context")
        if type(context) not in (ScopeContext, TenantWideScopeContext):
            raise TypeError("context must be a recognized scope context")
        if definition.workspace_scoped and type(context) is TenantWideScopeContext:
            raise ValueError("workspace-scoped resources require a workspace context")
        return definition.to_definition()

    def validate_privileged_tenant_wide_binding(
        self,
        resource_name: str,
        context: TenantWideScopeContext,
        *,
        operation: ResourceOperation | None = None,
    ) -> ResourceScopeDefinition:
        """Explicitly validate privileged tenant-wide access to a tenant resource."""
        if type(context) is not TenantWideScopeContext:
            raise TypeError("context must be a TenantWideScopeContext")
        definition = self.__by_resource[resource_name]
        self._validate_operation(definition, operation)
        if definition.scope is ResourceScope.GLOBAL:
            raise ValueError("global resources do not accept a tenant context")
        if not definition.direct_scope_ready:
            raise ValueError(f"tenant resource {resource_name!r} has pending direct ownership")
        return definition.to_definition()

    @staticmethod
    def _validate_operation(
        definition: _ResourceScopeSnapshot,
        operation: ResourceOperation | None,
    ) -> None:
        if operation is None:
            return
        if type(operation) is not ResourceOperation:
            raise TypeError("operation must be a ResourceOperation")
        if operation not in definition.operations:
            raise ValueError(
                f"operation {operation.value!r} is not declared for {definition.resource_name!r}"
            )
