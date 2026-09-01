from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.cloud.keys_api import router as keys_router
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base, get_db
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def test_project_api_key_authenticates_sdk_routes_and_can_be_revoked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'keys.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(keys_router, prefix="/v1")
    app.include_router(cloud_router, prefix="/v1")

    def raw_db():
        with Session(engine) as db:
            yield db

    def jwt_scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_db] = raw_db
    app.dependency_overrides[get_current_scoped_db] = jwt_scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    jwt = mint_econ_service_token()
    assert jwt is not None
    jwt_headers = {"Authorization": f"Bearer {jwt}"}
    client = TestClient(app)

    created = client.post(
        "/v1/cloud/api-keys",
        headers=jwt_headers,
        json={"name": "production", "roles": ["Analyst"]},
    )

    assert created.status_code == 200, created.text
    secret = created.json()["api_key"]
    key_id = created.json()["key_id"]
    assert secret.startswith("zth_live_")
    assert created.json()["last_four"] == secret[-4:]

    accepted = client.post(
        "/v1/executions",
        headers={"Authorization": f"Bearer {secret}"},
        json={
            "workflow": "invoice-agent",
            "workflow_version": "v1",
            "run_id": "run-1",
            "step": "extract",
            "recorded_at": datetime(2026, 8, 31, tzinfo=UTC).isoformat(),
            "cost_usd": "0.02",
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "inserted"

    revoked = client.delete(f"/v1/cloud/api-keys/{key_id}", headers=jwt_headers)
    rejected = client.post(
        "/v1/executions",
        headers={"Authorization": f"Bearer {secret}"},
        json={
            "workflow": "invoice-agent",
            "run_id": "run-2",
            "step": "extract",
        },
    )

    assert revoked.status_code == 204
    assert rejected.status_code == 401


def test_unknown_project_api_key_is_rejected(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'unknown.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")

    def raw_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = raw_db

    response = TestClient(app).post(
        "/v1/executions",
        headers={"Authorization": "Bearer zth_live_unknown_secret"},
        json={"workflow": "invoice-agent", "run_id": "run-1", "step": "extract"},
    )

    assert response.status_code == 401
