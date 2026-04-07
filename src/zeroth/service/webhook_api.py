"""Webhook subscription CRUD and dead-letter management REST API.

Provides:
  POST   /webhooks/subscriptions                     -- Create subscription
  GET    /webhooks/subscriptions                     -- List subscriptions
  GET    /webhooks/subscriptions/{subscription_id}   -- Get subscription
  DELETE /webhooks/subscriptions/{subscription_id}   -- Deactivate subscription
  GET    /webhooks/dead-letters                      -- List dead-letter entries
  POST   /webhooks/dead-letters/{dead_letter_id}/replay -- Replay dead-letter
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.service.authorization import Permission, require_permission
from zeroth.webhooks.models import WebhookEventType, WebhookSubscription


class CreateSubscriptionRequest(BaseModel):
    """Request body for creating a webhook subscription."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    target_url: str
    event_types: list[str]
    tenant_id: str = "default"


class WebhookSubscriptionResponse(BaseModel):
    """Response for a single webhook subscription."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    deployment_ref: str
    tenant_id: str
    target_url: str
    secret: str
    event_types: list[str]
    active: bool
    created_at: str
    updated_at: str


class WebhookSubscriptionListResponse(BaseModel):
    """Response for listing webhook subscriptions."""

    model_config = ConfigDict(extra="forbid")

    subscriptions: list[WebhookSubscriptionResponse]
    total: int


class WebhookDeadLetterResponse(BaseModel):
    """Response for a single dead-letter entry."""

    model_config = ConfigDict(extra="forbid")

    dead_letter_id: str
    delivery_id: str
    subscription_id: str
    event_type: str
    event_id: str
    attempt_count: int
    last_error: str | None
    last_status_code: int | None
    created_at: str
    dead_lettered_at: str


class WebhookDeadLetterListResponse(BaseModel):
    """Response for listing dead-letter entries."""

    model_config = ConfigDict(extra="forbid")

    dead_letters: list[WebhookDeadLetterResponse]
    total: int


def _serialize_subscription(sub: WebhookSubscription) -> WebhookSubscriptionResponse:
    """Convert a WebhookSubscription model to an API response."""
    return WebhookSubscriptionResponse(
        subscription_id=sub.subscription_id,
        deployment_ref=sub.deployment_ref,
        tenant_id=sub.tenant_id,
        target_url=sub.target_url,
        secret=sub.secret,
        event_types=[e.value for e in sub.event_types],
        active=sub.active,
        created_at=sub.created_at.isoformat(),
        updated_at=sub.updated_at.isoformat(),
    )


def register_webhook_routes(app: FastAPI | APIRouter) -> None:
    """Register webhook subscription and dead-letter management routes."""

    @app.post(
        "/webhooks/subscriptions",
        response_model=WebhookSubscriptionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_subscription(
        request: Request,
        payload: CreateSubscriptionRequest,
    ) -> WebhookSubscriptionResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        sub = WebhookSubscription(
            deployment_ref=payload.deployment_ref,
            tenant_id=payload.tenant_id,
            target_url=payload.target_url,
            event_types=[WebhookEventType(e) for e in payload.event_types],
        )
        created = await webhook_service.create_subscription(sub)
        return _serialize_subscription(created)

    @app.get(
        "/webhooks/subscriptions",
        response_model=WebhookSubscriptionListResponse,
    )
    async def list_subscriptions(
        request: Request,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> WebhookSubscriptionListResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        subs = await webhook_service.list_subscriptions(
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
        )
        return WebhookSubscriptionListResponse(
            subscriptions=[_serialize_subscription(s) for s in subs],
            total=len(subs),
        )

    @app.get(
        "/webhooks/subscriptions/{subscription_id}",
        response_model=WebhookSubscriptionResponse,
    )
    async def get_subscription(
        request: Request,
        subscription_id: str,
    ) -> WebhookSubscriptionResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        sub = await webhook_service.get_subscription(subscription_id)
        if sub is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="subscription not found",
            )
        return _serialize_subscription(sub)

    @app.delete(
        "/webhooks/subscriptions/{subscription_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def deactivate_subscription(
        request: Request,
        subscription_id: str,
    ) -> None:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        await webhook_service.deactivate_subscription(subscription_id)

    @app.get(
        "/webhooks/dead-letters",
        response_model=WebhookDeadLetterListResponse,
    )
    async def list_dead_letters(
        request: Request,
        subscription_id: str | None = None,
        limit: int = 50,
    ) -> WebhookDeadLetterListResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        dead_letters = await webhook_service.list_dead_letters(
            subscription_id=subscription_id, limit=limit
        )
        items = [
            WebhookDeadLetterResponse(
                dead_letter_id=dl.dead_letter_id,
                delivery_id=dl.delivery_id,
                subscription_id=dl.subscription_id,
                event_type=dl.event_type.value,
                event_id=dl.event_id,
                attempt_count=dl.attempt_count,
                last_error=dl.last_error,
                last_status_code=dl.last_status_code,
                created_at=dl.created_at.isoformat(),
                dead_lettered_at=dl.dead_lettered_at.isoformat(),
            )
            for dl in dead_letters
        ]
        return WebhookDeadLetterListResponse(
            dead_letters=items,
            total=len(items),
        )

    @app.post(
        "/webhooks/dead-letters/{dead_letter_id}/replay",
        status_code=status.HTTP_201_CREATED,
    )
    async def replay_dead_letter(
        request: Request,
        dead_letter_id: str,
    ) -> dict[str, Any]:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        webhook_service = _webhook_service(request)
        try:
            delivery = await webhook_service.replay_dead_letter(dead_letter_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="dead letter not found",
            ) from exc
        return {
            "delivery_id": delivery.delivery_id,
            "status": delivery.status.value,
        }


def _webhook_service(request: Request) -> Any:
    """Extract the WebhookService from the bootstrap."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    webhook_service = getattr(bootstrap, "webhook_service", None)
    if webhook_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook service not available",
        )
    return webhook_service
