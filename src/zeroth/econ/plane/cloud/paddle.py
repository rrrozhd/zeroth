"""Paddle checkout, customer portal, and verified subscription projection."""

from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.billing.schemas import BillingSubscriptionEvent
from zeroth.econ.plane.billing.service import apply_subscription_event
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

router = APIRouter(prefix="/cloud/billing", tags=["zeroth-cloud-billing"])
_MAX_WEBHOOK_BYTES = 1_000_000


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["solo"]


class BillingURL(BaseModel):
    url: str


class PaddleGateway(Protocol):
    def create_checkout(self, *, price_id: str, tenant_id: str) -> str: ...

    def create_portal(self, *, customer_id: str, subscription_id: str | None) -> str: ...

    def verify_webhook(self, *, raw_body: bytes, signature: str) -> bool: ...


class PaddleSDKGateway:
    """Thin adapter around the optional official Paddle Billing SDK."""

    def __init__(self) -> None:
        try:
            import paddle_billing
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise RuntimeError("Install zeroth[cloud] to enable Paddle billing") from exc
        environment = (
            paddle_billing.Environment.SANDBOX
            if settings.paddle_sandbox
            else paddle_billing.Environment.PRODUCTION
        )
        self._client = paddle_billing.Client(
            settings.paddle_api_key,
            options=paddle_billing.Options(environment=environment),
        )

    def create_checkout(self, *, price_id: str, tenant_id: str) -> str:
        from paddle_billing.Entities.Shared import CustomData, TransactionStatus
        from paddle_billing.Resources.Transactions.Operations import CreateTransaction
        from paddle_billing.Resources.Transactions.Operations.Create import TransactionCreateItem

        transaction = self._client.transactions.create(
            CreateTransaction(
                items=[TransactionCreateItem(price_id=price_id, quantity=1)],
                status=TransactionStatus.Ready,
                custom_data=CustomData({"zeroth_tenant_id": tenant_id}),
            )
        )
        url = transaction.checkout.url if transaction.checkout else None
        if not url:
            raise RuntimeError("Paddle did not return a checkout URL")
        return url

    def create_portal(self, *, customer_id: str, subscription_id: str | None) -> str:
        from paddle_billing.Resources.CustomerPortalSessions.Operations import (
            CreateCustomerPortalSession,
        )

        operation = (
            CreateCustomerPortalSession(subscription_ids=[subscription_id])
            if subscription_id
            else CreateCustomerPortalSession()
        )
        session = self._client.customer_portal_sessions.create(customer_id, operation)
        return session.urls.general.overview

    def verify_webhook(self, *, raw_body: bytes, signature: str) -> bool:
        from paddle_billing.Notifications import Secret, Verifier

        request = SimpleNamespace(
            body=raw_body,
            headers={"Paddle-Signature": signature},
        )
        return bool(Verifier().verify(request, Secret(settings.paddle_webhook_secret)))


def get_paddle_gateway() -> PaddleGateway:
    return PaddleSDKGateway()


def require_paddle_enabled() -> None:
    if not settings.paddle_billing_enabled:
        raise HTTPException(status_code=404, detail="Hosted billing is disabled")


def _price_map() -> dict[str, Literal["solo", "team"]]:
    return {
        settings.paddle_solo_price_id: "solo",
        settings.paddle_team_price_id: "team",
    }


@router.post("/checkout", response_model=BillingURL)
def checkout(
    payload: CheckoutRequest,
    _enabled: None = Depends(require_paddle_enabled),  # noqa: B008
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    gateway: PaddleGateway = Depends(get_paddle_gateway),  # noqa: B008
) -> BillingURL:
    price_id = settings.paddle_solo_price_id
    if not price_id:
        raise HTTPException(status_code=503, detail="Billing price is not configured")
    return BillingURL(url=gateway.create_checkout(price_id=price_id, tenant_id=user.tenant_id))


@router.post("/portal", response_model=BillingURL)
def portal(
    _enabled: None = Depends(require_paddle_enabled),  # noqa: B008
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    gateway: PaddleGateway = Depends(get_paddle_gateway),  # noqa: B008
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
) -> BillingURL:
    subscription = db.get(CloudSubscription, user.tenant_id)
    if subscription is None or not subscription.external_customer_id:
        raise HTTPException(status_code=409, detail="No Paddle customer exists for this tenant")
    if subscription.billing_provider != "paddle":
        raise HTTPException(status_code=409, detail="Subscription is not managed by Paddle")
    return BillingURL(
        url=gateway.create_portal(
            customer_id=subscription.external_customer_id,
            subscription_id=subscription.external_subscription_id,
        )
    )


def _normalize_event(payload: dict[str, Any]) -> BillingSubscriptionEvent:
    data = payload["data"]
    custom_data = data.get("custom_data") or {}
    period = data.get("current_billing_period") or {}
    price_id = data["items"][0]["price"]["id"]
    plan = _price_map().get(price_id)
    if plan is None:
        raise ValueError("Paddle price is not recognized")
    tenant_id = custom_data.get("zeroth_tenant_id")
    if not tenant_id:
        raise ValueError("Paddle subscription is missing Zeroth tenant ownership")
    if not str(payload.get("event_type", "")).startswith("subscription."):
        raise ValueError("Paddle event is not a subscription event")
    return BillingSubscriptionEvent(
        provider="paddle",
        event_id=payload["event_id"],
        tenant_id=tenant_id,
        external_customer_id=data["customer_id"],
        external_subscription_id=data["id"],
        external_price_id=price_id,
        plan=plan,
        status=data["status"],
        period_start=datetime.fromisoformat(period["starts_at"]),
        period_end=datetime.fromisoformat(period["ends_at"]),
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
    )


@router.post("/paddle/webhook")
async def paddle_webhook(
    request: Request,
    _enabled: None = Depends(require_paddle_enabled),  # noqa: B008
    gateway: PaddleGateway = Depends(get_paddle_gateway),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict[str, str]:
    raw_body = await request.body()
    if len(raw_body) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Paddle webhook is too large")
    signature = request.headers.get("Paddle-Signature", "")
    if not signature or not gateway.verify_webhook(raw_body=raw_body, signature=signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Paddle signature")
    try:
        event = _normalize_event(json.loads(raw_body))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Paddle subscription event") from exc
    scoped = ScopedSession(db, TenantWideScopeContext(tenant_id=event.tenant_id))
    result = apply_subscription_event(scoped, event)
    return {"disposition": result.disposition}
