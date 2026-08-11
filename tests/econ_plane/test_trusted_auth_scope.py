from __future__ import annotations

import importlib.util
import inspect as python_inspect
from pathlib import Path
from typing import Annotated

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.api import router as auth_router
from zeroth.econ.plane.auth.models import Role, User
from zeroth.econ.plane.auth.schemas import UserClaims
from zeroth.econ.plane.auth.service import decode_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.costing.models import CalibrationMetric, GroundTruthCost
from zeroth.econ.plane.database import Base, get_db
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.reconciliation.api import router as reconciliation_router
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _auth_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix="/v1")

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return app


def test_standalone_public_token_issuer_is_disabled_by_default(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)

    response = TestClient(_auth_app(engine)).post(
        "/v1/auth/token",
        json={
            "sub": "attacker",
            "email": "attacker@example.com",
            "roles": ["Admin"],
            "tenant_id": "victim",
        },
    )

    assert settings.insecure_public_token_issuer_enabled is False
    assert response.status_code == 404


def test_service_token_claims_come_from_trusted_configuration(monkeypatch) -> None:
    monkeypatch.setattr(settings, "service_principal_subject", "trusted-worker")
    monkeypatch.setattr(settings, "service_principal_email", "worker@example.com")
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "service_principal_workspace_id", "workspace-7")
    monkeypatch.setattr(settings, "service_principal_roles", "Admin,Analyst")

    token = mint_econ_service_token()

    assert token is not None
    claims = decode_token(token)
    assert claims.sub == "trusted-worker"
    assert claims.email == "worker@example.com"
    assert claims.tenant_id == "tenant-a"
    assert claims.workspace_id == "workspace-7"
    assert claims.roles == ["Admin", "Analyst"]


def test_insecure_issuer_rejects_request_tenant_mismatch(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'mismatch.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = Role(name="Admin")
        db.add(
            User(
                tenant_id="tenant-a",
                workspace_id="workspace-a",
                subject="subject-1",
                email="subject@example.com",
                roles=[admin],
            )
        )
        db.commit()
    monkeypatch.setattr(settings, "insecure_public_token_issuer_enabled", True)

    response = TestClient(_auth_app(engine), raise_server_exceptions=False).post(
        "/v1/auth/token",
        json={
            "sub": "subject-1",
            "email": "subject@example.com",
            "roles": ["Admin"],
            "tenant_id": "tenant-b",
            "workspace_id": "workspace-a",
        },
    )

    assert response.status_code == 403


def test_protected_route_rejects_request_selected_tenant(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'route.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    token = mint_econ_service_token()
    assert token is not None

    response = TestClient(app).post(
        "/v1/instrumentation/executions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": "tenant-b",
            "execution_id": "cross-tenant",
            "timestamp": "2026-08-11T00:00:00Z",
            "capability_id": "cap",
            "implementation_id": "impl",
            "model_version": "v1",
        },
    )

    assert response.status_code == 403


def test_execution_rejects_foreign_metadata_tenant_even_when_top_level_matches(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'metadata-route.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    token = mint_econ_service_token()
    assert token is not None

    response = TestClient(app).post(
        "/v1/instrumentation/executions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": "tenant-a",
            "execution_id": "metadata-cross-tenant",
            "timestamp": "2026-08-11T00:00:00Z",
            "capability_id": "cap",
            "implementation_id": "impl",
            "model_version": "v1",
            "metadata": {"tenant_id": "tenant-b"},
        },
    )

    assert response.status_code == 403


def _dependency_calls(route: APIRoute) -> set[object]:
    pending = list(route.dependant.dependencies)
    calls: set[object] = set()
    while pending:
        dependency = pending.pop()
        calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    return calls


def test_every_operational_econ_database_route_requires_auth_and_structural_scope() -> None:
    from zeroth.econ.plane.auth.deps import (
        get_current_global_db,
        get_current_scoped_db,
        get_current_user,
    )
    from zeroth.econ.plane.main import app

    operational = []
    non_operational_database_routes = {"/v1/auth/token", "/v1/metrics"}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = _dependency_calls(route)
        has_database_parameter = "db" in python_inspect.signature(route.endpoint).parameters
        if has_database_parameter and route.path not in non_operational_database_routes:
            operational.append(route)
            assert get_current_user in calls, route.path
            assert calls.intersection({get_current_scoped_db, get_current_global_db}), route.path

    assert operational


def _tenant_reconciliation_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(reconciliation_router, prefix="/v1")

    def scoped_db(user: Annotated[UserClaims, Depends(get_current_user)]):
        with Session(engine) as db:
            scope = TenantWideScopeContext(tenant_id=user.tenant_id)
            yield ScopedSession(db, scope)

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    return app


def test_standalone_reconciliation_requires_authentication(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reconcile-auth.db'}")
    Base.metadata.create_all(engine)

    response = TestClient(_tenant_reconciliation_app(engine)).get(
        "/v1/reconciliation/calibration-summary"
    )

    assert response.status_code in {401, 403}


def test_reconciliation_writes_and_reads_only_authenticated_tenant(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reconcile-scope.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                CalibrationMetric(
                    tenant_id="tenant-a",
                    period="2026-08",
                    capability_id="visible-a",
                ),
                CalibrationMetric(
                    tenant_id="tenant-b",
                    period="2026-08",
                    capability_id="hidden-b",
                ),
            ]
        )
        db.commit()
    app = _tenant_reconciliation_app(engine)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        imported = client.post(
            "/v1/reconciliation/ground-truth-import",
            headers=headers,
            json={
                "rows": [
                    {
                        "period_start": "2026-08-01T00:00:00Z",
                        "period_end": "2026-08-31T00:00:00Z",
                        "capability_id": "cap-a",
                        "component": "llm",
                        "amount_usd": 12.5,
                    }
                ]
            },
        )
        summary = client.get("/v1/reconciliation/calibration-summary", headers=headers)

    assert imported.status_code == 200, imported.text
    assert [row["capability_id"] for row in summary.json()] == ["visible-a"]
    with Session(engine) as db:
        persisted = db.query(GroundTruthCost).one()
        assert persisted.tenant_id == "tenant-a"


def _load_auth_scope_migration():
    path = (
        Path(__file__).parents[2]
        / "src/zeroth/econ/plane/_migrations/versions/20260811_04_auth_scope.py"
    )
    spec = importlib.util.spec_from_file_location("auth_scope_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _auth_scope_shape(bind) -> dict[str, object]:
    inspector = inspect(bind)
    scope_columns = {"tenant_id", "workspace_id"}
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    indexes = {
        tuple(index["column_names"]): (index["name"], bool(index["unique"]))
        for index in inspector.get_indexes("users")
        if set(index["column_names"]) <= scope_columns
    }
    unique_constraints = sorted(
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("users")
        if set(constraint["column_names"]) & scope_columns
    )
    foreign_keys = sorted(
        tuple(constraint["constrained_columns"])
        for constraint in inspector.get_foreign_keys("users")
        if set(constraint["constrained_columns"]) & scope_columns
    )
    check_constraints = sorted(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("users")
        if any(column in constraint["sqltext"] for column in scope_columns)
    )
    return {
        "tenant_nullable": columns["tenant_id"]["nullable"],
        "tenant_default": columns["tenant_id"]["default"],
        "workspace_nullable": columns["workspace_id"]["nullable"],
        "workspace_default": columns["workspace_id"]["default"],
        "indexes": indexes,
        "unique_constraints": unique_constraints,
        "foreign_keys": foreign_keys,
        "check_constraints": check_constraints,
    }


def test_auth_scope_migration_matches_fresh_schema_and_backfills_reserved_default() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), "
                "subject VARCHAR(128) NOT NULL, email VARCHAR(255) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, tenant_id, subject, email) VALUES "
                "(1, NULL, 'a', 'a@example.com'), "
                "(2, 'tenant_default', 'b', 'b@example.com')"
            )
        )
        migration = _load_auth_scope_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        migrated_shape = _auth_scope_shape(connection)
        rows = connection.execute(
            text("SELECT tenant_id, workspace_id FROM users ORDER BY id")
        ).all()

    fresh = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(fresh)
    fresh_shape = _auth_scope_shape(fresh)

    assert migrated_shape == fresh_shape
    assert rows == [("default", None), ("default", None)]


def test_fresh_auth_schema_matches_migrated_scope_shape() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    columns = {column["name"]: column for column in inspect(engine).get_columns("users")}

    assert columns["tenant_id"]["nullable"] is False
    assert columns["tenant_id"]["default"] is None
    assert columns["workspace_id"]["nullable"] is True
    assert User.__table__.c.tenant_id.default.arg == "default"
