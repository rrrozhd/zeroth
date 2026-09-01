from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.cloud.activation import VerifiedWorkOSIdentity, activate_trial
from zeroth.econ.plane.cloud.auth import get_cloud_user
from zeroth.econ.plane.cloud.models import CloudApiKey
from zeroth.econ.plane.database import Base, get_db


@dataclass
class _Paddle:
    checkout_tenant: str | None = None

    def create_checkout(self, *, price_id: str, tenant_id: str) -> str:
        assert price_id == "pri_solo"
        self.checkout_tenant = tenant_id
        return "https://checkout.paddle.test/txn_01"

    def create_portal(self, *, customer_id: str, subscription_id: str | None) -> str:
        raise AssertionError("trial account has no Paddle portal")

    def verify_webhook(self, *, raw_body: bytes, signature: str) -> bool:
        raise AssertionError("webhook is not part of the browser account surface")


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, sessionmaker, _Paddle, str]:
    from zeroth.econ.plane.cloud.paddle import get_paddle_gateway
    from zeroth.econ.plane.cloud.web import router
    from zeroth.econ.plane.config import settings

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'self-service.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as db:
        activation = activate_trial(
            db,
            VerifiedWorkOSIdentity(
                external_user_id="user_01",
                external_organization_id="org_01",
                email="owner@example.com",
                email_verified=True,
            ),
        )
    paddle = _Paddle()
    app = FastAPI()
    app.include_router(router)

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_cloud_user] = lambda: ScopedUserClaims(
        sub="workos:user_01",
        email="owner@example.com",
        roles=["Admin"],
        tenant_id=activation.tenant_id,
        workspace_id=None,
        exp=253402300799,
        iss="workos-authkit",
    )
    app.dependency_overrides[get_paddle_gateway] = lambda: paddle
    monkeypatch.setattr(settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(settings, "paddle_billing_enabled", True)
    monkeypatch.setattr(settings, "paddle_solo_price_id", "pri_solo")
    monkeypatch.setattr(settings, "cloud_browser_origin", "https://api.example.test")
    return TestClient(app, base_url="https://api.example.test"), sessions, paddle, activation.tenant_id


def test_public_root_explains_the_single_offer_and_starts_authkit(tmp_path: Path, monkeypatch) -> None:
    client, _, _, _ = _client(tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert "Economic debugger for production AI" in response.text
    assert "$39/month" in response.text
    assert "14-day trial" in response.text
    assert 'href="/v1/cloud/auth/login"' in response.text
    assert "Team" not in response.text


def test_account_shows_plan_and_key_fingerprint_without_revealing_secret(
    tmp_path: Path, monkeypatch
) -> None:
    client, sessions, _, _ = _client(tmp_path, monkeypatch)
    with sessions() as db:
        row = db.query(CloudApiKey).one()
        last_four = row.last_four
        secret_hash = row.secret_hash

    response = client.get("/account")

    assert response.status_code == 200
    assert "Trial" in response.text
    assert last_four in response.text
    assert secret_hash not in response.text
    assert 'action="/account/api-keys"' in response.text
    assert 'action="/account/checkout"' in response.text


def test_account_checkout_form_redirects_to_paddle_using_trusted_tenant(
    tmp_path: Path, monkeypatch
) -> None:
    client, _, paddle, tenant_id = _client(tmp_path, monkeypatch)

    response = client.post(
        "/account/checkout",
        headers={"Origin": "https://api.example.test"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.paddle.test/txn_01"
    assert paddle.checkout_tenant == tenant_id


def test_account_can_replace_a_lost_key_and_revoke_the_old_key(
    tmp_path: Path, monkeypatch
) -> None:
    client, sessions, _, _ = _client(tmp_path, monkeypatch)
    with sessions() as db:
        old_key_id = db.query(CloudApiKey).one().key_id

    reveal = client.post(
        "/account/api-keys",
        headers={"Origin": "https://api.example.test"},
    )
    revoked = client.post(
        f"/account/api-keys/{old_key_id}/revoke",
        headers={"Origin": "https://api.example.test"},
        follow_redirects=False,
    )

    assert reveal.status_code == 200
    assert reveal.headers["cache-control"] == "no-store"
    assert "Copy this key now" in reveal.text
    assert "zth_live_" in reveal.text
    assert revoked.status_code == 303
    assert revoked.headers["location"] == "/account"
    with sessions() as db:
        assert db.get(CloudApiKey, old_key_id).revoked_at is not None
