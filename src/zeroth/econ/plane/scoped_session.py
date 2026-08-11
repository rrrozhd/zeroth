"""Tenant-scoped SQLAlchemy boundary for econ persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql.dml import Delete, Insert, Update

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
    for description in getattr(state.statement, "column_descriptions", ()):
        entity = description.get("entity")
        inspected = inspect(entity, raiseerr=False) if entity is not None else None
        if inspected is not None and hasattr(inspected, "class_"):
            models[inspected.class_] = None
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


def _reject_ownership_rewrite(statement: Update | Delete, context: ScopeBinding) -> None:
    if not isinstance(statement, Update):
        return
    values = _statement_values(statement)
    if isinstance(context, (ScopeContext, TenantWideScopeContext)):
        if "tenant_id" in values and values["tenant_id"] != context.tenant_id:
            raise ValueError("tenant ownership does not match the bound scope")
    if isinstance(context, ScopeContext):
        if "workspace_id" in values and values["workspace_id"] != context.workspace_id:
            raise ValueError("workspace ownership does not match the bound scope")


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


def _scope_statement(state: ORMExecuteState) -> None:
    if _SCOPE_INFO_KEY not in state.session.info:
        return
    context: ScopeBinding = state.session.info[_SCOPE_INFO_KEY]
    operation = _operation_for_statement(state)
    models = _models_for_statement(state)
    if not models:
        raise ValueError("scoped SQL execution must target a mapped resource")
    for model in models:
        definition = _definition(model)
        _validate_binding(definition, context, operation)
        if state.is_insert:
            _scope_insert(state, definition, context)
            continue
        if isinstance(state.statement, (Update, Delete)):
            _reject_ownership_rewrite(state.statement, context)
        if definition.scope is ResourceScope.GLOBAL:
            continue
        assert context is not None
        tenant_id = context.tenant_id
        state.statement = state.statement.options(
            with_loader_criteria(
                model,
                lambda entity: entity.tenant_id == tenant_id,
                include_aliases=True,
            )
        )
        if definition.workspace_scoped:
            assert isinstance(context, ScopeContext)
            workspace_id = context.workspace_id
            state.statement = state.statement.options(
                with_loader_criteria(
                    model,
                    lambda entity: entity.workspace_id == workspace_id,
                    include_aliases=True,
                )
            )


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

    def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        return self.__session.execute(statement, params=params, **kwargs)

    def scalars(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        return self.__session.scalars(statement, params=params, **kwargs)

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
