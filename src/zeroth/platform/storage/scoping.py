"""Authoritative resource scope definitions for persistent storage."""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, TypeVar, final

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


_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])
type PersistenceProbe = Callable[[Any, ResourceOperation], Awaitable[None]]


def named_isolation_probe(probe_name: str) -> PersistenceProbe:
    """Create a lazy probe reference that avoids repository import cycles."""
    _require_non_empty_string(probe_name, "probe_name")

    async def execute(database: Any, operation: ResourceOperation) -> None:
        module_name, separator, function_name = probe_name.partition(":")
        if not separator:
            module_name = "zeroth.service.persistence_isolation_probes"
            function_name = probe_name
        probes = importlib.import_module(module_name)
        probe = getattr(probes, function_name)
        await probe(database, operation)

    return execute


def persistence_operation(
    *operations: ResourceOperation,
) -> Callable[[_CallableT], _CallableT]:
    """Attach immutable, explicit persistence semantics to a public method."""
    if not operations:
        raise ValueError("persistence operations must be non-empty")
    if any(type(operation) is not ResourceOperation for operation in operations):
        raise TypeError("persistence operations must contain ResourceOperation members")
    declared = frozenset(operations)

    def decorate(method: _CallableT) -> _CallableT:
        if not callable(method):
            raise TypeError("persistence_operation can decorate only callables")
        if hasattr(method, "__persistence_operations__"):
            raise ValueError("persistence operations are already declared")
        method.__persistence_operations__ = declared  # type: ignore[attr-defined]
        return method

    return decorate


def persistence_resource_operations(
    resource_name: str, *operations: ResourceOperation
) -> Callable[[_CallableT], _CallableT]:
    """Bind one multi-resource repository method to a resource's operations."""
    _require_stable_resource_name(resource_name)
    if not operations:
        raise ValueError("resource persistence operations must be non-empty")
    declared = frozenset(operations)

    def decorate(method: _CallableT) -> _CallableT:
        resource_operations = dict(getattr(method, "__persistence_resource_operations__", {}))
        if resource_name in resource_operations:
            raise ValueError(f"operations for {resource_name} are already declared")
        resource_operations[resource_name] = declared
        method.__persistence_resource_operations__ = MappingProxyType(  # type: ignore[attr-defined]
            resource_operations
        )
        return method

    return decorate


@dataclass(frozen=True, slots=True)
class _PersistenceSurfaceDeclaration:
    resource_name: str
    probe: PersistenceProbe | None
    non_persistence_public_methods: frozenset[str]
    method_names: frozenset[str] | None


_PERSISTENCE_REPOSITORY_TYPES: set[type[Any]] = set()


def persistence_surface(
    resource_name: str,
    *,
    probe: PersistenceProbe | None = None,
    non_persistence_public_methods: frozenset[str] = frozenset(),
    method_names: frozenset[str] | None = None,
) -> Callable[[type[Any]], type[Any]]:
    """Declare a production repository surface adjacent to its implementation."""
    _require_stable_resource_name(resource_name)

    def decorate(repository_type: type[Any]) -> type[Any]:
        declarations = list(getattr(repository_type, "__persistence_surface_declarations__", ()))
        if any(item.resource_name == resource_name for item in declarations):
            raise ValueError(f"surface {resource_name} is already declared")
        declarations.append(
            _PersistenceSurfaceDeclaration(
                resource_name,
                probe,
                non_persistence_public_methods,
                method_names,
            )
        )
        repository_type.__persistence_surface_declarations__ = tuple(declarations)
        _PERSISTENCE_REPOSITORY_TYPES.add(repository_type)
        return repository_type

    return decorate


@dataclass(frozen=True, slots=True)
class PersistenceSurface:
    """Production repository class bound to one registered resource."""

    resource_name: str
    repository_type: type[Any]
    operation_methods: Mapping[str, frozenset[ResourceOperation]]
    non_persistence_public_methods: frozenset[str] = frozenset()
    probe: PersistenceProbe | None = None


_PERSISTENCE_SURFACES: dict[tuple[str, type[Any]], PersistenceSurface] = {}


def discover_persistence_surfaces() -> tuple[PersistenceSurface, ...]:
    """Rebuild surfaces by introspecting decorated production repository classes."""
    _PERSISTENCE_SURFACES.clear()
    for repository_type in sorted(
        _PERSISTENCE_REPOSITORY_TYPES,
        key=lambda item: (item.__module__, item.__qualname__),
    ):
        declarations: tuple[_PersistenceSurfaceDeclaration, ...] = (
            repository_type.__persistence_surface_declarations__
        )
        single_resource = len(declarations) == 1
        for declaration in declarations:
            operation_methods: dict[str, frozenset[ResourceOperation]] = {}
            for method_name, method in vars(repository_type).items():
                operations = getattr(method, "__persistence_operations__", None)
                if not operations:
                    continue
                resource_operations = getattr(method, "__persistence_resource_operations__", {})
                selected = resource_operations.get(declaration.resource_name)
                if declaration.method_names is not None and selected is None:
                    selected = operations if method_name in declaration.method_names else None
                if (
                    selected is None
                    and declaration.method_names is None
                    and (single_resource or not resource_operations)
                ):
                    selected = operations
                if selected is not None:
                    operation_methods[method_name] = selected
            _PERSISTENCE_SURFACES[(declaration.resource_name, repository_type)] = (
                PersistenceSurface(
                    declaration.resource_name,
                    repository_type,
                    MappingProxyType(operation_methods),
                    declaration.non_persistence_public_methods,
                    declaration.probe,
                )
            )
    return persistence_surfaces()


def register_persistence_surface(
    resource_name: str,
    repository_type: type[Any],
    *,
    operation_methods: Mapping[str, frozenset[ResourceOperation]],
    non_persistence_public_methods: frozenset[str] = frozenset(),
) -> PersistenceSurface:
    """Register reviewed production method metadata and fail on drift."""
    _require_stable_resource_name(resource_name)
    for method_name, operations in operation_methods.items():
        method = vars(repository_type).get(method_name)
        if not callable(method):
            raise TypeError(f"{repository_type.__name__}.{method_name} is not a public method")
        if not operations:
            raise ValueError("persistence method operations must be non-empty")
        existing = getattr(method, "__persistence_operations__", None)
        if existing is None:
            persistence_operation(*operations)(method)
        elif existing != operations:
            method.__persistence_operations__ = frozenset(existing | operations)  # type: ignore[attr-defined]
        resource_operations = dict(getattr(method, "__persistence_resource_operations__", {}))
        previous = resource_operations.get(resource_name)
        if previous is not None and previous != operations:
            raise ValueError(
                f"{repository_type.__name__}.{method_name} has conflicting resource metadata"
            )
        resource_operations[resource_name] = operations
        method.__persistence_resource_operations__ = MappingProxyType(  # type: ignore[attr-defined]
            resource_operations
        )
    surface = PersistenceSurface(
        resource_name,
        repository_type,
        dict(operation_methods),
        non_persistence_public_methods,
    )
    _PERSISTENCE_SURFACES[(resource_name, repository_type)] = surface
    return surface


def persistence_surfaces() -> tuple[PersistenceSurface, ...]:
    """Return registered production repository surfaces in stable order."""
    return tuple(
        sorted(
            _PERSISTENCE_SURFACES.values(),
            key=lambda item: (item.resource_name, item.repository_type.__qualname__),
        )
    )


def validate_persistence_surface(
    surface: PersistenceSurface,
    definition: ResourceScopeDefinition | None = None,
) -> None:
    """Reject undecorated public methods and empty or unexpected declarations."""
    class_surfaces = tuple(
        item
        for item in _PERSISTENCE_SURFACES.values()
        if item.repository_type is surface.repository_type
    )
    expected_by_method: dict[str, set[ResourceOperation]] = {}
    for item in class_surfaces:
        for name, operations in item.operation_methods.items():
            expected_by_method.setdefault(name, set()).update(operations)
    non_persistence_methods = frozenset(
        name for item in class_surfaces for name in item.non_persistence_public_methods
    )
    declared_operations: set[ResourceOperation] = set()
    for name, method in vars(surface.repository_type).items():
        if name.startswith("_") or not callable(method):
            continue
        operations = getattr(method, "__persistence_operations__", None)
        if name in non_persistence_methods:
            if operations is not None:
                raise AssertionError(f"non-persistence method {name} has operation metadata")
            continue
        if not operations:
            raise AssertionError(f"public persistence method {name} lacks operation metadata")
        if not all(type(operation) is ResourceOperation for operation in operations):
            raise AssertionError(f"public persistence method {name} has invalid metadata")
        if name not in expected_by_method:
            raise AssertionError(
                f"public persistence method {name} is missing from registered surfaces"
            )
        if set(operations) != expected_by_method[name]:
            raise AssertionError(f"public persistence method {name} has wrong metadata")
        expected = surface.operation_methods.get(name)
        if expected is not None:
            resource_operations = getattr(method, "__persistence_resource_operations__", {}).get(
                surface.resource_name
            )
            if resource_operations is not None and resource_operations != expected:
                raise AssertionError(
                    f"public persistence method {name} has wrong resource metadata"
                )
            declared_operations.update(expected)
    if definition is not None:
        if definition.resource_name != surface.resource_name:
            raise AssertionError("surface and definition resource names differ")
        extras = declared_operations - set(definition.operations)
        if extras:
            raise AssertionError(f"surface declares extra operations: {sorted(extras)}")


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
class NullWorkspaceScopeContext:
    """A tenant identity explicitly bound to resources outside any workspace."""

    tenant_id: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.tenant_id, "tenant_id")
        if self.tenant_id == _DEFAULT_TENANT_ID:
            raise ValueError("the reserved default tenant requires for_default_compatibility()")

    @classmethod
    def for_default_compatibility(cls) -> NullWorkspaceScopeContext:
        """Build the reserved default tenant's null-workspace context."""
        context = object.__new__(cls)
        object.__setattr__(context, "tenant_id", _DEFAULT_TENANT_ID)
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


@dataclass(frozen=True, slots=True, init=False)
@final
class CrossTenantMaintenanceScopeContext:
    """Explicit read/enumerate authority for scheduled maintenance."""

    allowed_resource_names: ClassVar[frozenset[str]] = frozenset(
        {"service.approvals", "service.retention_policies"}
    )

    @classmethod
    def for_scheduled_maintenance(cls) -> CrossTenantMaintenanceScopeContext:
        return object.__new__(cls)


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


type ScopeBinding = ScopeContext | NullWorkspaceScopeContext | TenantWideScopeContext | None


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
        if type(context) not in (ScopeContext, NullWorkspaceScopeContext, TenantWideScopeContext):
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

    def validate_cross_tenant_maintenance_binding(
        self,
        resource_name: str,
        context: CrossTenantMaintenanceScopeContext,
        *,
        operation: ResourceOperation,
    ) -> ResourceScopeDefinition:
        """Validate read-only cross-tenant maintenance enumeration."""
        if type(context) is not CrossTenantMaintenanceScopeContext:
            raise TypeError("context must be a CrossTenantMaintenanceScopeContext")
        if operation not in {ResourceOperation.READ, ResourceOperation.ENUMERATE}:
            raise ValueError("cross-tenant maintenance is read-only")
        if resource_name not in context.allowed_resource_names:
            raise ValueError("cross-tenant maintenance is limited to approved resources")
        definition = self.__by_resource[resource_name]
        self._validate_operation(definition, operation)
        if definition.scope is ResourceScope.GLOBAL:
            raise ValueError("global resources do not accept a maintenance context")
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
