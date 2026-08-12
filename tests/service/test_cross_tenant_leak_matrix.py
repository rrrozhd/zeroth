"""Registry-generated cross-tenant isolation proof for physical resources."""

from __future__ import annotations

import pytest
import httpx
import sqlite3
import tempfile
from pathlib import Path
import importlib
import pkgutil
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.service.cross_tenant_resource_factory import (
    exercise_relational_case,
    exercise_sqlalchemy_case,
    generated_cross_tenant_cases,
    generated_sqlalchemy_cases,
    physical_table_names,
    seed_sqlalchemy_mapping,
    validate_resource_inventory,
)
from tests.task9_operation_driver_registry import TASK9_EXECUTABLE_DRIVERS
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    ResourceOperation,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
)
from zeroth.service.api.studio_api import router as studio_router
from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.platform.artifacts.errors import ArtifactNotFoundError
from zeroth.platform.artifacts.store import FilesystemArtifactStore
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore
from zeroth.platform.secrets.vault import TenantScopedVaultDriver, VaultSecretProvider
from zeroth.platform.storage.scoped_resource import ScopedResourceDriver
from zeroth.econ.plane import __path__ as econ_plane_paths
from zeroth.econ.plane.database import Base as EconBase
from zeroth.econ.plane import scoped_session as scoped_session_module
from zeroth.platform.storage.scoped_table import _StructuredTable


def _econ_models() -> tuple[type, ...]:
    for module in pkgutil.walk_packages(econ_plane_paths, prefix="zeroth.econ.plane."):
        if module.name.endswith(".models"):
            importlib.import_module(module.name)
    return tuple(
        mapper.class_
        for mapper in EconBase.registry.mappers
        if mapper.class_.__module__.startswith("zeroth.econ.plane.")
        and mapper.class_.__module__.endswith(".models")
    )


_ECON_MODELS = _econ_models()
_ECON_COLLECTION_ENGINE = create_engine("sqlite://")
EconBase.metadata.create_all(_ECON_COLLECTION_ENGINE)
ECON_CASES = generated_sqlalchemy_cases(_ECON_MODELS, _ECON_COLLECTION_ENGINE)

_SERVICE_SCHEMA_DIRECTORY = tempfile.TemporaryDirectory(prefix="zeroth-cross-tenant-matrix-")
_SERVICE_SCHEMA_PATH = Path(_SERVICE_SCHEMA_DIRECTORY.name) / "service.db"
run_migrations(f"sqlite:///{_SERVICE_SCHEMA_PATH}")
with sqlite3.connect(_SERVICE_SCHEMA_PATH) as _connection:
    _SERVICE_PHYSICAL_TABLES = {
        str(row[0])
        for row in _connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
SERVICE_CASES = generated_cross_tenant_cases(SERVICE_SCOPE_REGISTRY, _SERVICE_PHYSICAL_TABLES)


def test_physical_resource_registration_generates_crud_parameter_ids_without_case_edit() -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Widget(MetaBase):
        __tablename__ = "meta_widgets"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.widgets",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )

        widget_id: Mapped[str] = mapped_column(String, primary_key=True)
        tenant_id: Mapped[str] = mapped_column(String, nullable=False)
        value: Mapped[str] = mapped_column(String, nullable=False)

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)

    ids = {case.parameter_id for case in generated_sqlalchemy_cases([Widget], engine)}

    assert ids == {f"meta.widgets:{operation.value}" for operation in ResourceOperation}


def test_sqlalchemy_seed_generation_recursively_satisfies_foreign_keys() -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Parent(MetaBase):
        __tablename__ = "meta_parents"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.parents",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )
        parent_id: Mapped[str] = mapped_column(String, primary_key=True)
        tenant_id: Mapped[str] = mapped_column(String, nullable=False)

    class Child(MetaBase):
        __tablename__ = "meta_children"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.children",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )
        child_id: Mapped[str] = mapped_column(String, primary_key=True)
        tenant_id: Mapped[str] = mapped_column(String, nullable=False)
        parent_id: Mapped[str] = mapped_column(ForeignKey("meta_parents.parent_id"))

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    values = seed_sqlalchemy_mapping(engine, Child, tenant_id="tenant-a", token="child")

    assert values["parent_id"] == "matrix-child-parent_id"
    case = next(
        item
        for item in generated_sqlalchemy_cases([Parent, Child], engine)
        if item.definition.resource_name == "meta.children"
        and item.operation is ResourceOperation.READ
    )
    exercise_sqlalchemy_case(engine, case)


def test_sqlalchemy_create_case_detects_disabled_ownership_injection(monkeypatch) -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Widget(MetaBase):
        __tablename__ = "meta_create_mutation_widgets"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.create_mutation_widgets",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )
        tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
        widget_id: Mapped[str] = mapped_column(String, primary_key=True)
        value: Mapped[str] = mapped_column(String, nullable=False)

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    case = next(
        item
        for item in generated_sqlalchemy_cases([Widget], engine)
        if item.operation is ResourceOperation.CREATE
    )
    monkeypatch.setattr(scoped_session_module, "_fill_or_verify", lambda *args, **kwargs: None)

    with pytest.raises(IntegrityError):
        exercise_sqlalchemy_case(engine, case)


def test_sqlalchemy_update_case_detects_disabled_tenant_predicate(monkeypatch) -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Widget(MetaBase):
        __tablename__ = "meta_update_mutation_widgets"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.update_mutation_widgets",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )
        tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
        widget_id: Mapped[str] = mapped_column(String, primary_key=True)
        value: Mapped[str] = mapped_column(String, nullable=False)

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    case = next(
        item
        for item in generated_sqlalchemy_cases([Widget], engine)
        if item.operation is ResourceOperation.UPDATE
    )
    monkeypatch.setattr(scoped_session_module, "_apply_tenant_criteria", lambda *args: None)

    with pytest.raises(AssertionError):
        exercise_sqlalchemy_case(engine, case)


@pytest.mark.parametrize(
    "operation",
    (ResourceOperation.READ, ResourceOperation.ENUMERATE, ResourceOperation.DELETE),
    ids=lambda operation: operation.value,
)
def test_sqlalchemy_query_case_detects_disabled_tenant_predicate(operation, monkeypatch) -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Widget(MetaBase):
        __tablename__ = "meta_query_mutation_widgets"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.query_mutation_widgets",
            table_name=__tablename__,
            operations=frozenset(ResourceOperation),
        )
        tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
        widget_id: Mapped[str] = mapped_column(String, primary_key=True)
        value: Mapped[str] = mapped_column(String, nullable=False)

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    case = next(
        item for item in generated_sqlalchemy_cases([Widget], engine) if item.operation is operation
    )
    monkeypatch.setattr(scoped_session_module, "_apply_tenant_criteria", lambda *args: None)

    with pytest.raises(AssertionError):
        exercise_sqlalchemy_case(engine, case)


def test_sqlalchemy_create_case_requires_a_retrievable_foreign_row() -> None:
    class MetaBase(DeclarativeBase):
        pass

    class Widget(MetaBase):
        __tablename__ = "meta_create_widgets"
        scope_definition = ResourceScopeDefinition(
            resource_name="meta.create_widgets",
            table_name=__tablename__,
            operations=frozenset({ResourceOperation.CREATE, ResourceOperation.READ}),
        )
        widget_id: Mapped[str] = mapped_column(String, primary_key=True)
        tenant_id: Mapped[str] = mapped_column(String, nullable=False)

    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    case = next(
        item
        for item in generated_sqlalchemy_cases([Widget], engine)
        if item.operation is ResourceOperation.CREATE
    )

    exercise_sqlalchemy_case(engine, case)


def test_definition_without_physical_resource_fails_inventory() -> None:
    registry = ResourceScopeRegistry(
        [
            ResourceScopeDefinition(
                resource_name="meta.bare",
                table_name="missing_table",
                operations=frozenset({ResourceOperation.READ}),
            )
        ]
    )

    with pytest.raises(AssertionError, match="missing physical tenant resources"):
        validate_resource_inventory(registry, set())


async def test_registry_matches_physical_service_inventory(async_database) -> None:
    validate_resource_inventory(SERVICE_SCOPE_REGISTRY, await physical_table_names(async_database))


def test_non_relational_production_drivers_expose_immutable_real_operation_sets(tmp_path) -> None:
    artifact = TenantScopedArtifactStore(FilesystemArtifactStore(tmp_path), tenant_id="tenant-a")
    vault = TenantScopedVaultDriver(
        VaultSecretProvider(
            addr="https://vault.test",
            token="token",
            transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        ),
        tenant_id="tenant-a",
    )

    assert isinstance(artifact, ScopedResourceDriver)
    assert isinstance(vault, ScopedResourceDriver)
    assert set(artifact.operations) == {
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.UPDATE,
        ResourceOperation.DELETE,
    }
    assert set(vault.operations) == {ResourceOperation.READ, ResourceOperation.ENUMERATE}
    with pytest.raises(TypeError):
        artifact.operations[ResourceOperation.READ] = artifact.retrieve  # type: ignore[index]


async def test_artifact_scoped_resource_driver_invokes_production_methods(tmp_path) -> None:
    backend = FilesystemArtifactStore(tmp_path)
    owner = TenantScopedArtifactStore(backend, tenant_id="tenant-a")
    foreign = TenantScopedArtifactStore(backend, tenant_id="tenant-b")

    await owner.operations[ResourceOperation.CREATE]("run/key", b"owner", "text/plain")
    assert await owner.operations[ResourceOperation.READ]("run/key") == b"owner"
    assert await owner.operations[ResourceOperation.UPDATE]("run/key", 120) is True
    with pytest.raises(ArtifactNotFoundError):
        await foreign.operations[ResourceOperation.READ]("run/key")
    assert (
        await foreign.operations[ResourceOperation.DELETE](
            "run/key", idempotency_key="foreign-delete"
        )
        is False
    )
    assert await owner.retrieve("run/key") == b"owner"


async def test_vault_scoped_resource_driver_invokes_bound_production_methods() -> None:
    values = {
        "/v1/secret/data/tenants/tenant-a/shared": "owner",
        "/v1/secret/data/tenants/tenant-b/shared": "foreign",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        value = values.get(request.url.path)
        return (
            httpx.Response(404)
            if value is None
            else httpx.Response(200, json={"data": {"data": {"value": value}}})
        )

    provider = VaultSecretProvider(
        addr="https://vault.test", token="token", async_transport=httpx.MockTransport(handler)
    )
    owner = TenantScopedVaultDriver(provider, tenant_id="tenant-a")
    foreign = TenantScopedVaultDriver(provider, tenant_id="tenant-b")
    try:
        assert await owner.operations[ResourceOperation.READ]("shared") == "owner"
        assert await foreign.operations[ResourceOperation.READ]("shared") == "foreign"
        assert await foreign.operations[ResourceOperation.ENUMERATE](["shared", "missing"]) == {
            "shared": "foreign"
        }
    finally:
        await provider.aclose()


@pytest.mark.parametrize("case", SERVICE_CASES, ids=lambda case: case.parameter_id)
async def test_registry_generated_cross_tenant_case(async_database, case) -> None:
    driver = TASK9_EXECUTABLE_DRIVERS.get((case.definition.resource_name, case.operation))
    if driver is not None:
        await driver(async_database)
        return
    await exercise_relational_case(async_database, SERVICE_SCOPE_REGISTRY, case)


@pytest.mark.parametrize("case", ECON_CASES, ids=lambda case: case.parameter_id)
def test_registry_generated_sqlalchemy_cross_tenant_case(case) -> None:
    engine = create_engine("sqlite://")
    EconBase.metadata.create_all(engine)
    exercise_sqlalchemy_case(engine, case)


@pytest.mark.parametrize("operation", list(ResourceOperation), ids=lambda item: item.value)
async def test_each_disabled_gateway_predicate_is_detected(
    tmp_path, operation, monkeypatch
) -> None:
    from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase

    database = AsyncSQLiteDatabase(str(tmp_path / f"mutation-{operation.value}.db"))
    async with database.transaction() as connection:
        await connection.execute_script(
            """
            CREATE TABLE matrix_rows (
                row_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (tenant_id, row_id)
            );
            """
        )
    definition = ResourceScopeDefinition(
        resource_name="meta.matrix_rows",
        table_name="matrix_rows",
        operations=frozenset(ResourceOperation),
    )
    registry = ResourceScopeRegistry([definition])
    case = next(
        item
        for item in generated_cross_tenant_cases(registry, {"matrix_rows"})
        if item.operation is operation
    )

    if operation is ResourceOperation.CREATE:
        original_validate = _StructuredTable._validate_values

        def without_create_binding(self, values, *, create, definition):
            return original_validate(self, values, create=False, definition=definition)

        monkeypatch.setattr(_StructuredTable, "_validate_values", without_create_binding)
    else:
        original_where = _StructuredTable._where

        def without_scope(self, where, *, qualifier=None, include_scope=True, definition):
            return original_where(
                self,
                where,
                qualifier=qualifier,
                include_scope=False,
                definition=definition,
            )

        monkeypatch.setattr(_StructuredTable, "_where", without_scope)

    expected_failure = Exception if operation is ResourceOperation.CREATE else AssertionError
    match = "tenant_id" if operation is ResourceOperation.CREATE else None
    with pytest.raises(expected_failure, match=match):
        await exercise_relational_case(database, registry, case)


# API masking remains supplemental: the generated matrix proves persistence
# boundaries, while these cases prove the public surface does not disclose
# whether a hidden workflow exists.
def _studio_app(repo: GraphRepository, tenant_id: str) -> FastAPI:
    app = FastAPI()
    bootstrap = type("Bootstrap", (), {})()
    bootstrap.graph_repository = repo
    bootstrap.audit_repository = None
    app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject=f"{tenant_id}-admin",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.ADMIN],
        tenant_id=tenant_id,
    )

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(studio_router)
    return app


def _assert_masked(foreign, unknown) -> None:
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
async def test_workflow_foreign_identity_matches_unknown(sqlite_db, operation) -> None:
    repo = GraphRepository(sqlite_db)
    with (
        TestClient(_studio_app(repo, "tenant-a")) as owner,
        TestClient(_studio_app(repo, "tenant-b")) as foreign,
    ):
        graph_id = owner.post("/api/studio/v1/workflows", json={"name": "private"}).json()["id"]
        if operation == "read":
            request = foreign.get
        elif operation == "update":

            def request(path: str):
                return foreign.post(f"{path}/publish")
        else:
            request = foreign.delete
        _assert_masked(
            request(f"/api/studio/v1/workflows/{graph_id}"),
            request("/api/studio/v1/workflows/unknown-workflow"),
        )


async def test_workflow_foreign_enumeration_matches_unknown_tenant(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    with (
        TestClient(_studio_app(repo, "tenant-a")) as owner,
        TestClient(_studio_app(repo, "tenant-b")) as foreign,
        TestClient(_studio_app(repo, "tenant-unknown")) as unknown,
    ):
        owner.post("/api/studio/v1/workflows", json={"name": "private"})
        assert (
            foreign.get("/api/studio/v1/workflows").json()
            == unknown.get("/api/studio/v1/workflows").json()
        )
