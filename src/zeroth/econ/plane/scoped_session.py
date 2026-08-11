"""Tenant-scoped SQLAlchemy boundary for econ persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.selectable import TableClause

from zeroth.platform.storage.scoping import (
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ScopeBinding,
    ScopeContext,
    TenantWideScopeContext,
)

_SCOPE_INFO_KEY = "zeroth_econ_scope_binding"
_MISSING = object()
_ModelT = TypeVar("_ModelT")


def _definition(model: type) -> ResourceScopeDefinition:
    definition = vars(model).get("scope_definition")
    if type(definition) is not ResourceScopeDefinition:
        raise TypeError(f"mapped class {model.__qualname__} has no scope definition")
    return definition


def _models_for_statement(state: ORMExecuteState) -> tuple[type, ...]:
    models: dict[type, None] = {}
    if state.bind_mapper is not None:
        models[state.bind_mapper.class_] = None
    for element in visitors.iterate(state.statement):
        for description in getattr(element, "column_descriptions", ()):
            entity = description.get("entity")
            inspected = inspect(entity, raiseerr=False) if entity is not None else None
            if inspected is None:
                continue
            mapper = getattr(inspected, "mapper", inspected)
            if hasattr(mapper, "class_"):
                models[mapper.class_] = None
    return tuple(models)


def _registered_models(state: ORMExecuteState, referenced: tuple[type, ...]) -> tuple[type, ...]:
    registries: dict[object, None] = {}
    if state.bind_mapper is not None:
        registries[state.bind_mapper.registry] = None
    for model in referenced:
        inspected = inspect(model, raiseerr=False)
        if inspected is not None and hasattr(inspected, "registry"):
            registries[inspected.registry] = None
    models: dict[type, None] = {}
    for registry in registries:
        for mapper in registry.mappers:
            if type(vars(mapper.class_).get("scope_definition")) is ResourceScopeDefinition:
                models[mapper.class_] = None
    return tuple(models)


def _operation_for_statement(state: ORMExecuteState) -> ResourceOperation:
    if state.is_insert:
        return ResourceOperation.CREATE
    if state.is_update:
        return ResourceOperation.UPDATE
    if state.is_delete:
        return ResourceOperation.DELETE
    return ResourceOperation.READ


def _validate_binding(
    definition: ResourceScopeDefinition,
    context: ScopeBinding,
    operation: ResourceOperation,
) -> None:
    if operation not in definition.operations:
        raise ValueError(
            f"operation {operation.value!r} is not declared for {definition.resource_name!r}"
        )
    if definition.scope is ResourceScope.GLOBAL:
        if context is not None:
            raise ValueError("global resources do not accept a tenant binding")
        return
    if context is None:
        raise ValueError("tenant-scoped resources require a tenant binding")
    if type(context) not in (ScopeContext, TenantWideScopeContext):
        raise TypeError("context must be a recognized scope context")
    if definition.workspace_scoped and type(context) is TenantWideScopeContext:
        raise ValueError("workspace-scoped resources require a workspace context")


def _statement_values(statement: Insert | Update | Delete) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in (getattr(statement, "_values", None) or {}).items():
        column_name = getattr(key, "key", key if isinstance(key, str) else None)
        if column_name is not None:
            values[column_name] = getattr(value, "value", value)
    return values


def _mapping_has_ownership(values: Mapping[Any, Any]) -> bool:
    names = {getattr(key, "key", key) for key in values}
    return bool(names & {"tenant_id", "workspace_id"})


def _reject_update_ownership(state: ORMExecuteState) -> None:
    statement = state.statement
    if not isinstance(statement, Update):
        return
    if _mapping_has_ownership(getattr(statement, "_values", None) or {}):
        raise ValueError("tenant ownership is immutable; workspace ownership is immutable")
    ordered_values = getattr(statement, "_ordered_values", None) or ()
    if _mapping_has_ownership(dict(ordered_values)):
        raise ValueError("tenant ownership is immutable; workspace ownership is immutable")
    parameters = state.parameters
    if isinstance(parameters, Mapping):
        mappings = (parameters,)
    elif isinstance(parameters, (list, tuple)) and all(
        isinstance(item, Mapping) for item in parameters
    ):
        mappings = parameters
    else:
        mappings = ()
    if any(_mapping_has_ownership(values) for values in mappings):
        raise ValueError("tenant ownership is immutable; workspace ownership is immutable")


def _normalize_insert_mapping(
    values: Mapping[Any, Any],
    definition: ResourceScopeDefinition,
    context: ScopeBinding,
) -> dict[Any, Any]:
    normalized = dict(values)
    by_name = {getattr(key, "key", key): value for key, value in values.items()}
    if definition.scope is ResourceScope.GLOBAL:
        if "tenant_id" in by_name or "workspace_id" in by_name:
            raise ValueError("global resources cannot declare ownership")
        return normalized
    assert context is not None
    tenant_id = by_name.get("tenant_id", _MISSING)
    if tenant_id is not _MISSING and tenant_id != context.tenant_id:
        raise ValueError("tenant ownership does not match the bound scope")
    if tenant_id is _MISSING:
        normalized["tenant_id"] = context.tenant_id
    if definition.workspace_scoped:
        assert isinstance(context, ScopeContext)
        workspace_id = by_name.get("workspace_id", _MISSING)
        if workspace_id is not _MISSING and workspace_id != context.workspace_id:
            raise ValueError("workspace ownership does not match the bound scope")
        if workspace_id is _MISSING:
            normalized["workspace_id"] = context.workspace_id
    return normalized


def _scope_insert(
    state: ORMExecuteState,
    definition: ResourceScopeDefinition,
    context: ScopeBinding,
) -> None:
    statement = state.statement
    assert isinstance(statement, Insert)
    if statement._multi_values:
        raise ValueError("mapped multi-values INSERT is not safely scopable; use executemany")

    embedded_values = _statement_values(statement)
    normalized_embedded = _normalize_insert_mapping(embedded_values, definition, context)
    added_values = {
        key: value for key, value in normalized_embedded.items() if key not in embedded_values
    }
    if added_values:
        state.statement = statement.values(**added_values)

    parameters = state.parameters
    if parameters is None:
        return
    if isinstance(parameters, Mapping):
        state.parameters = _normalize_insert_mapping(parameters, definition, context)
        return
    if isinstance(parameters, (list, tuple)) and all(
        isinstance(item, Mapping) for item in parameters
    ):
        state.parameters = [
            _normalize_insert_mapping(item, definition, context) for item in parameters
        ]
        return
    raise ValueError("mapped INSERT parameters must be mappings")


def _validate_select_shape(
    state: ORMExecuteState,
    registered: tuple[type, ...],
) -> None:
    statement = state.statement
    if type(statement).__name__ == "FromStatement" or any(
        isinstance(element, TextClause) for element in visitors.iterate(statement)
    ):
        raise ValueError("scoped execution requires a scopable ORM SELECT")

    tenant_tables = {
        inspect(model).local_table.name
        for model in registered
        if _definition(model).scope is ResourceScope.TENANT_SCOPED
    }
    for selectable in visitors.iterate(statement):
        descriptions = getattr(selectable, "column_descriptions", None)
        final_froms = getattr(selectable, "get_final_froms", None)
        if descriptions is None or final_froms is None:
            continue
        orm_tables: set[str] = set()
        for description in descriptions:
            entity = description.get("entity")
            inspected = inspect(entity, raiseerr=False) if entity is not None else None
            mapper = getattr(inspected, "mapper", inspected)
            if mapper is not None and hasattr(mapper, "local_table"):
                orm_tables.add(mapper.local_table.name)
            expression = description.get("expr")
            table = getattr(expression, "table", None)
            if (
                getattr(table, "name", None) in tenant_tables
                and not getattr(expression, "_annotations", {})
            ):
                raise ValueError("scoped execution requires a scopable ORM SELECT")
        for from_clause in final_froms():
            for element in visitors.iterate(from_clause):
                table_name = getattr(element, "name", None)
                if (
                    table_name in tenant_tables
                    and table_name not in orm_tables
                    and not getattr(element, "_annotations", {})
                ):
                    raise ValueError("scoped execution requires a scopable ORM SELECT")


def _validate_no_textual_sql(
    state: ORMExecuteState,
    registered: tuple[type, ...],
) -> None:
    governed_tables = {inspect(model).local_table.name for model in registered}
    for element in visitors.iterate(state.statement):
        class_name = type(element).__name__.lower()
        if isinstance(element, TextClause) or "textual" in class_name:
            raise ValueError(
                "scoped execution requires a scopable ORM SELECT and rejects textual SQL constructs"
            )
        if not isinstance(element, ColumnClause) or getattr(element, "_annotations", {}):
            continue
        table = getattr(element, "table", None)
        table_name = getattr(table, "name", None)
        if (
            element.is_literal
            or table_name is None
            or type(table) is TableClause
            or table_name in governed_tables
        ):
            raise ValueError(
                "scoped execution requires a scopable ORM SELECT and rejects textual SQL constructs"
            )


def _apply_tenant_criteria(
    state: ORMExecuteState,
    models: tuple[type, ...],
    context: ScopeBinding,
) -> None:
    if context is None:
        return
    for model in models:
        definition = _definition(model)
        if definition.scope is ResourceScope.GLOBAL:
            continue
        tenant_id = context.tenant_id
        state.statement = state.statement.options(
            with_loader_criteria(
                model,
                lambda entity: entity.tenant_id == tenant_id,
                include_aliases=True,
            )
        )
        if definition.workspace_scoped and isinstance(context, ScopeContext):
            workspace_id = context.workspace_id
            state.statement = state.statement.options(
                with_loader_criteria(
                    model,
                    lambda entity: entity.workspace_id == workspace_id,
                    include_aliases=True,
                )
            )


def _scope_statement(state: ORMExecuteState) -> None:
    if _SCOPE_INFO_KEY not in state.session.info:
        return
    context: ScopeBinding = state.session.info[_SCOPE_INFO_KEY]
    operation = _operation_for_statement(state)
    models = _models_for_statement(state)
    if not models:
        raise ValueError("scoped SQL execution must target a mapped resource")
    registered = _registered_models(state, models)
    _validate_no_textual_sql(state, registered)
    for model in models:
        definition = _definition(model)
        _validate_binding(definition, context, operation)
        if state.is_insert:
            _scope_insert(state, definition, context)
            continue
    if state.is_insert:
        return
    if state.is_update:
        _reject_update_ownership(state)
        if isinstance(state.parameters, (list, tuple)):
            state.update_execution_options(synchronize_session=False, dml_strategy="orm")
    if state.is_select or state.is_from_statement:
        _validate_select_shape(state, registered)
        _apply_tenant_criteria(state, registered, context)
    else:
        _apply_tenant_criteria(state, models, context)


def _ownership_value(instance: object, name: str) -> Any:
    return getattr(instance, name, None)


def _verify_original_ownership(instance: object, name: str, expected: str) -> None:
    state = inspect(instance)
    attribute = state.attrs.get(name)
    if attribute is None:
        return
    for previous in attribute.history.deleted:
        if previous is not None and previous != expected:
            raise ValueError(f"{name.removesuffix('_id')} ownership does not match the bound scope")


def _fill_or_verify(instance: object, name: str, expected: str, *, is_new: bool) -> None:
    _verify_original_ownership(instance, name, expected)
    actual = _ownership_value(instance, name)
    if is_new and actual is None:
        setattr(instance, name, expected)
        return
    if actual != expected:
        raise ValueError(f"{name.removesuffix('_id')} ownership does not match the bound scope")


def _instance_matches_scope(instance: object, context: ScopeBinding) -> bool:
    definition = _definition(type(instance))
    _validate_binding(definition, context, ResourceOperation.READ)
    if definition.scope is ResourceScope.GLOBAL:
        return _ownership_value(instance, "tenant_id") is None and _ownership_value(
            instance, "workspace_id"
        ) is None
    assert context is not None
    if _ownership_value(instance, "tenant_id") != context.tenant_id:
        return False
    if definition.workspace_scoped:
        assert isinstance(context, ScopeContext)
        return _ownership_value(instance, "workspace_id") == context.workspace_id
    return True


def _validate_pending_ownership(session: Session, _flush_context: object, _instances: object) -> None:
    if _SCOPE_INFO_KEY not in session.info:
        return
    context: ScopeBinding = session.info[_SCOPE_INFO_KEY]
    new = set(session.new)
    pending: Iterable[object] = new | set(session.dirty) | set(session.deleted)
    for instance in pending:
        definition = _definition(type(instance))
        operation = ResourceOperation.CREATE if instance in new else ResourceOperation.UPDATE
        if instance in session.deleted:
            operation = ResourceOperation.DELETE
        _validate_binding(definition, context, operation)
        if definition.scope is ResourceScope.GLOBAL:
            if _ownership_value(instance, "tenant_id") is not None or _ownership_value(
                instance, "workspace_id"
            ) is not None:
                raise ValueError("global resources cannot declare ownership")
            continue
        assert context is not None
        _fill_or_verify(instance, "tenant_id", context.tenant_id, is_new=instance in new)
        if definition.workspace_scoped:
            assert isinstance(context, ScopeContext)
            _fill_or_verify(instance, "workspace_id", context.workspace_id, is_new=instance in new)


event.listen(Session, "do_orm_execute", _scope_statement)
event.listen(Session, "before_flush", _validate_pending_ownership)


class ScopedScalarResult:
    """Restricted scalar-result view with no connection escape surface."""

    __slots__ = ("__result",)

    def __init__(self, result: Any) -> None:
        self.__result = result

    def __iter__(self):
        return iter(self.__result)

    def all(self) -> list[Any]:
        return self.__result.all()

    def first(self) -> Any:
        return self.__result.first()

    def one(self) -> Any:
        return self.__result.one()

    def one_or_none(self) -> Any:
        return self.__result.one_or_none()


class ScopedResult:
    """Restricted ORM result view with no cursor or connection exposure."""

    __slots__ = ("__result",)

    def __init__(self, result: Any) -> None:
        self.__result = result

    def __iter__(self):
        return iter(self.__result)

    def all(self) -> list[Any]:
        return self.__result.all()

    def first(self) -> Any:
        return self.__result.first()

    def one(self) -> Any:
        return self.__result.one()

    def one_or_none(self) -> Any:
        return self.__result.one_or_none()

    def scalar(self) -> Any:
        return self.__result.scalar()

    def scalar_one(self) -> Any:
        return self.__result.scalar_one()

    def scalar_one_or_none(self) -> Any:
        return self.__result.scalar_one_or_none()

    def scalars(self, index: int = 0) -> ScopedScalarResult:
        return ScopedScalarResult(self.__result.scalars(index))


class ScopedSession:
    """Small service-facing facade over a scope-bound private SQLAlchemy session."""

    __slots__ = ("__session", "scope")

    def __init__(self, session: Session, scope: ScopeBinding) -> None:
        if type(session) is not Session and not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        if scope is not None and type(scope) not in (ScopeContext, TenantWideScopeContext):
            raise TypeError("scope must be a recognized scope context")
        if _SCOPE_INFO_KEY in session.info:
            raise ValueError("session is already scope-bound")
        if session.identity_map:
            raise ValueError("session identity map must be empty before scope binding")
        session.info[_SCOPE_INFO_KEY] = scope
        self.__session = session
        self.scope = scope

    def get(self, entity: type[_ModelT], ident: Any) -> _ModelT | None:
        definition = _definition(entity)
        _validate_binding(definition, self.scope, ResourceOperation.READ)
        instance = self.__session.get(entity, ident)
        if instance is not None and not _instance_matches_scope(instance, self.scope):
            return None
        return instance

    def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> ScopedResult:
        return ScopedResult(self.__session.execute(statement, params=params, **kwargs))

    def scalars(self, statement: Any, params: Any = None, **kwargs: Any) -> ScopedScalarResult:
        return ScopedScalarResult(self.__session.scalars(statement, params=params, **kwargs))

    def add(self, instance: object) -> None:
        self.__session.add(instance)

    def delete(self, instance: object) -> None:
        self.__session.delete(instance)

    def commit(self) -> None:
        self.__session.commit()

    def flush(self, objects: Iterable[object] | None = None) -> None:
        self.__session.flush(objects=objects)

    def rollback(self) -> None:
        self.__session.rollback()

    def refresh(self, instance: object, attribute_names: Iterable[str] | None = None) -> None:
        if not _instance_matches_scope(instance, self.scope):
            definition = _definition(type(instance))
            ownership = "workspace" if definition.workspace_scoped else "tenant"
            raise ValueError(f"{ownership} ownership does not match the bound scope")
        self.__session.refresh(instance, attribute_names=attribute_names)
