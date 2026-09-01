"""One commercial journey across hosted identity, value, and billing boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.backtesting.api import get_backtest_executor
from zeroth.econ.plane.backtesting.api import router as backtest_router
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.backtesting.schemas import BacktestComputation
from zeroth.econ.plane.billing.models import BillingEventReceipt
from zeroth.econ.plane.cloud import authkit
from zeroth.econ.plane.cloud.authkit import get_workos_gateway
from zeroth.econ.plane.cloud.authkit import router as authkit_router
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.cloud.paddle import get_paddle_gateway
from zeroth.econ.plane.cloud.paddle import router as paddle_router
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base, get_db


@dataclass
class _User:
    id: str = "user_owner"
    email: str = "owner@example.com"
    email_verified: bool = True
    first_name: str = "Solo"
    last_name: str = "Developer"


@dataclass
class _Auth:
    user: _User
    organization_id: str = "org_customer"
    access_token: str = "access"
    refresh_token: str = "refresh"
    impersonator: object | None = None


@dataclass
class _SessionAuth:
    authenticated: bool = True
    organization_id: str = "org_customer"
    role: str = "admin"
    roles: list[str] | None = None
    user: dict[str, str] | None = None


class _WorkOS:
    def __init__(self) -> None:
        self.state = ""

    def authorization_url(self, **kwargs: object) -> str:
        self.state = str(kwargs["state"])
        return "https://auth.example.test/authorize"

    def authenticate_with_code(self, *, code: str, code_verifier: str) -> _Auth:
        assert code == "valid-code" and code_verifier
        return _Auth(user=_User())

    def seal_session(self, auth: _Auth) -> str:
        assert auth.organization_id == "org_customer"
        return "sealed-session"

    def authenticate_session(self, sealed_session: str) -> _SessionAuth:
        assert sealed_session == "sealed-session"
        return _SessionAuth(user={"id": "user_owner", "email": "owner@example.com"})

    def create_organization(self, *, name: str, external_id: str) -> str:
        raise AssertionError("the test identity already belongs to an organization")

    def add_organization_member(self, *, user_id: str, organization_id: str, role: str) -> None:
        raise AssertionError("the test identity already belongs to an organization")

    def switch_organization(self, *, refresh_token: str, organization_id: str) -> _Auth:
        raise AssertionError("the test identity already belongs to an organization")


class _Paddle:
    def __init__(self) -> None:
        self.checkout_tenant = ""
        self.portal_subscription = ""

    def create_checkout(self, *, price_id: str, tenant_id: str) -> str:
        assert price_id == "pri_solo"
        self.checkout_tenant = tenant_id
        return "https://checkout.paddle.test/txn_01"

    def create_portal(self, *, customer_id: str, subscription_id: str | None) -> str:
        assert customer_id == "ctm_01"
        self.portal_subscription = str(subscription_id)
        return "https://customer-portal.paddle.test/session_01"

    def verify_webhook(self, *, raw_body: bytes, signature: str) -> bool:
        assert json.loads(raw_body)["event_id"].startswith("evt_")
        return signature == "ts=1;h1=signed"


class _BacktestExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _payload):  # type: ignore[no-untyped-def]
        self.calls += 1
        return BacktestComputation(
            incumbent_success_rate=0.96,
            candidate_success_rate=0.96,
            candidate_error_rate=0.0,
            savings_pct=45.0,
            provider_calls=20,
        )


def _backtest_request(*, candidate: str = "openai/gpt-5-nano"):  # type: ignore[no-untyped-def]
    from zeroth.protocol import BacktestCase, BacktestRequest, EconomicConstraints

    return BacktestRequest(
        workflow="invoice-agent",
        baseline_version="v7",
        node_id="extract",
        incumbent_model="openai/gpt-5-mini",
        instruction="Extract invoice fields.",
        candidate={"model": candidate},
        cases=[
            BacktestCase(
                id=f"invoice-{index}",
                input={"text": f"invoice {index}"},
                expected={"total": str(index)},
            )
            for index in range(5)
        ],
        constraints=EconomicConstraints(min_success_rate=0.95),
    )


def _subscription_event(
    *,
    tenant_id: str,
    event_id: str,
    status: str,
    occurred_at: datetime,
    event_verb: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": f"subscription.{event_verb or status}",
            "occurred_at": occurred_at.isoformat(),
            "data": {
                "id": "sub_01",
                "status": status,
                "customer_id": "ctm_01",
                "custom_data": {"zeroth_tenant_id": tenant_id},
                "items": [{"price": {"id": "pri_solo"}}],
                "current_billing_period": {
                    "starts_at": occurred_at.isoformat(),
                    "ends_at": (occurred_at + timedelta(days=30)).isoformat(),
                },
            },
        }
    ).encode()


def test_signup_backtest_upgrade_portal_and_cancellation(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'commercial-flow.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    workos = _WorkOS()
    paddle = _Paddle()
    executor = _BacktestExecutor()

    app = FastAPI()
    app.include_router(authkit_router, prefix="/v1")
    app.include_router(backtest_router, prefix="/v1")
    app.include_router(paddle_router, prefix="/v1")

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_workos_gateway] = lambda: workos
    app.dependency_overrides[get_paddle_gateway] = lambda: paddle
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(authkit, "get_workos_gateway", lambda: workos)
    monkeypatch.setattr(settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "workos_redirect_uri", "https://api.example.test/callback")
    monkeypatch.setattr(settings, "workos_cookie_password", "x" * 32)
    monkeypatch.setattr(settings, "cloud_browser_origin", "https://app.example.test")
    monkeypatch.setattr(settings, "paddle_billing_enabled", True)
    monkeypatch.setattr(settings, "paddle_solo_price_id", "pri_solo")
    monkeypatch.setattr(settings, "paddle_team_price_id", "pri_team")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)

    client = TestClient(app, base_url="https://api.example.test")
    login = client.get("/v1/cloud/auth/login", follow_redirects=False)
    assert login.status_code == 307
    activation = client.get(
        f"/v1/cloud/auth/callback?code=valid-code&state={workos.state}"
    )
    assert activation.status_code == 200, activation.text
    tenant_id = activation.json()["tenant_id"]
    api_key = activation.json()["api_key"]
    assert api_key.startswith("zth_live_")

    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "packaging" / "sdk" / "src"))
    from zeroth.sdk import ZerothClient

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    sdk = ZerothClient(
        api_key=api_key,
        base_url="https://api.example.test",
        http_client=httpx.Client(transport=httpx.MockTransport(dispatch)),
    )
    trial_backtest = sdk.create_backtest(_backtest_request())
    assert trial_backtest["recommended_action"] == "approve_candidate"
    assert sdk.list_backtests() == [trial_backtest]

    browser_headers = {"Origin": "https://app.example.test"}
    checkout = client.post(
        "/v1/cloud/billing/checkout",
        headers=browser_headers,
        json={"plan": "solo"},
    )
    assert checkout.status_code == 200, checkout.text
    assert paddle.checkout_tenant == tenant_id

    activated_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    active_event = _subscription_event(
        tenant_id=tenant_id,
        event_id="evt_active",
        status="active",
        occurred_at=activated_at,
        event_verb="activated",
    )
    active = client.post(
        "/v1/cloud/billing/paddle/webhook",
        content=active_event,
        headers={"Paddle-Signature": "ts=1;h1=signed"},
    )
    assert active.status_code == 200, active.text
    assert active.json() == {"disposition": "applied"}

    portal = client.post("/v1/cloud/billing/portal", headers=browser_headers)
    assert portal.status_code == 200, portal.text
    assert paddle.portal_subscription == "sub_01"

    canceled_event = _subscription_event(
        tenant_id=tenant_id,
        event_id="evt_canceled",
        status="canceled",
        occurred_at=activated_at + timedelta(days=1),
    )
    canceled = client.post(
        "/v1/cloud/billing/paddle/webhook",
        content=canceled_event,
        headers={"Paddle-Signature": "ts=1;h1=signed"},
    )
    assert canceled.status_code == 200, canceled.text

    with pytest.raises(httpx.HTTPStatusError) as denied:
        sdk.create_backtest(_backtest_request(candidate="openai/gpt-5-mini-2026-09"))
    assert denied.value.response.status_code == 402
    assert denied.value.response.json()["detail"] == "active Zeroth Cloud subscription required"
    assert executor.calls == 1

    with sessions() as db:
        subscription = db.get(CloudSubscription, tenant_id)
        assert subscription is not None and subscription.status == "canceled"
        assert len(list(db.scalars(select(BillingEventReceipt)))) == 2
        assert len(list(db.scalars(select(EconomicBacktestRecord)))) == 1
