from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_user
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.database import Base, get_db


class _FakePaddle:
    def __init__(self) -> None:
        self.checkout_calls: list[tuple[str, str]] = []
        self.portal_calls: list[tuple[str, str | None]] = []
        self.verified: list[tuple[bytes, str]] = []
        self.valid_signature = True

    def create_checkout(self, *, price_id: str, tenant_id: str) -> str:
        self.checkout_calls.append((price_id, tenant_id))
        return "https://checkout.paddle.test/txn_01"

    def create_portal(self, *, customer_id: str, subscription_id: str | None) -> str:
        self.portal_calls.append((customer_id, subscription_id))
        return "https://customer-portal.paddle.test/session_01"

    def verify_webhook(self, *, raw_body: bytes, signature: str) -> bool:
        self.verified.append((raw_body, signature))
        return self.valid_signature


def _client(tmp_path: Path, monkeypatch, gateway: _FakePaddle) -> tuple[TestClient, sessionmaker]:
    from zeroth.econ.plane.cloud.paddle import get_paddle_gateway, router
    from zeroth.econ.plane.config import settings

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'paddle.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_cloud_user] = lambda: ScopedUserClaims(
        sub="workos:user_01",
        email="owner@example.com",
        roles=["Admin"],
        tenant_id="tenant-a",
        workspace_id=None,
        exp=253402300799,
        iss="workos-authkit",
    )
    app.dependency_overrides[get_paddle_gateway] = lambda: gateway
    monkeypatch.setattr(settings, "paddle_solo_price_id", "pri_solo")
    monkeypatch.setattr(settings, "paddle_team_price_id", "pri_team")
    monkeypatch.setattr(settings, "paddle_billing_enabled", True)
    return TestClient(app), sessions


def test_checkout_maps_published_plan_to_server_price_and_trusted_tenant(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakePaddle()
    client, _ = _client(tmp_path, monkeypatch, gateway)

    response = client.post("/v1/cloud/billing/checkout", json={"plan": "solo"})

    assert response.status_code == 200
    assert response.json() == {"url": "https://checkout.paddle.test/txn_01"}
    assert gateway.checkout_calls == [("pri_solo", "tenant-a")]


def test_billing_route_is_inert_when_paddle_is_disabled(tmp_path: Path, monkeypatch) -> None:
    from zeroth.econ.plane.config import settings

    gateway = _FakePaddle()
    client, _ = _client(tmp_path, monkeypatch, gateway)
    monkeypatch.setattr(settings, "paddle_billing_enabled", False)

    response = client.post("/v1/cloud/billing/checkout", json={"plan": "solo"})

    assert response.status_code == 404
    assert gateway.checkout_calls == []


def test_checkout_rejects_request_selected_tenant(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakePaddle()
    client, _ = _client(tmp_path, monkeypatch, gateway)

    response = client.post(
        "/v1/cloud/billing/checkout",
        json={"plan": "solo", "tenant_id": "tenant-attacker"},
    )

    assert response.status_code == 422
    assert gateway.checkout_calls == []


def test_team_is_not_a_purchasable_self_serve_plan(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakePaddle()
    client, _ = _client(tmp_path, monkeypatch, gateway)

    response = client.post("/v1/cloud/billing/checkout", json={"plan": "team"})

    assert response.status_code == 422
    assert gateway.checkout_calls == []


def test_non_admin_cannot_change_billing(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakePaddle()
    client, _ = _client(tmp_path, monkeypatch, gateway)
    client.app.dependency_overrides[get_cloud_user] = lambda: ScopedUserClaims(
        sub="workos:user_02",
        email="analyst@example.com",
        roles=["Analyst"],
        tenant_id="tenant-a",
        workspace_id=None,
        exp=253402300799,
        iss="workos-authkit",
    )

    response = client.post("/v1/cloud/billing/checkout", json={"plan": "solo"})

    assert response.status_code == 403
    assert gateway.checkout_calls == []


def test_portal_uses_projected_paddle_customer_and_subscription(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakePaddle()
    client, sessions = _client(tmp_path, monkeypatch, gateway)
    now = datetime.now(UTC)
    with sessions() as db:
        db.add(
            CloudSubscription(
                tenant_id="tenant-a",
                plan="solo",
                status="active",
                period_start=now,
                period_end=now + timedelta(days=30),
                external_customer_id="ctm_01",
                external_subscription_id="sub_01",
                billing_provider="paddle",
                external_price_id="pri_solo",
                last_billing_event_id="evt_01",
                last_billing_event_at=now,
                updated_at=now,
            )
        )
        db.commit()

    response = client.post("/v1/cloud/billing/portal")

    assert response.status_code == 200
    assert gateway.portal_calls == [("ctm_01", "sub_01")]


def _paddle_event(**changes: object) -> bytes:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    payload: dict[str, object] = {
        "event_id": "evt_01",
        "event_type": "subscription.activated",
        "occurred_at": now.isoformat(),
        "data": {
            "id": "sub_01",
            "status": "active",
            "customer_id": "ctm_01",
            "custom_data": {"zeroth_tenant_id": "tenant-a"},
            "items": [{"price": {"id": "pri_solo"}}],
            "current_billing_period": {
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(days=30)).isoformat(),
            },
        },
    }
    payload.update(changes)
    return json.dumps(payload).encode()


def test_verified_webhook_projects_subscription_using_server_price_map(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakePaddle()
    client, sessions = _client(tmp_path, monkeypatch, gateway)
    raw = _paddle_event()

    response = client.post(
        "/v1/cloud/billing/paddle/webhook",
        content=raw,
        headers={"Paddle-Signature": "ts=1;h1=signed", "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert gateway.verified == [(raw, "ts=1;h1=signed")]
    with sessions() as db:
        subscription = db.get(CloudSubscription, "tenant-a")
        assert subscription is not None
        assert subscription.plan == "solo"
        assert subscription.billing_provider == "paddle"


def test_invalid_webhook_signature_is_rejected_before_json_or_state(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakePaddle()
    gateway.valid_signature = False
    client, sessions = _client(tmp_path, monkeypatch, gateway)

    response = client.post(
        "/v1/cloud/billing/paddle/webhook",
        content=b"not-json",
        headers={"Paddle-Signature": "invalid"},
    )

    assert response.status_code == 400
    with sessions() as db:
        assert db.get(CloudSubscription, "tenant-a") is None


def test_paddle_routes_are_mounted_on_the_plane() -> None:
    from zeroth.econ.plane.main import app

    paths = app.openapi()["paths"]
    assert "/v1/cloud/billing/checkout" in paths
    assert "/v1/cloud/billing/portal" in paths
    assert "/v1/cloud/billing/paddle/webhook" in paths
