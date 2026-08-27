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

import json
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from zeroth.platform.primitives.boundary import (
    OutboundDestinationError,
    validate_outbound_url,
)
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)
from zeroth.service.service_audit import ServiceAuditRecorder
from zeroth.service.webhooks.models import WebhookEventType, WebhookSubscription


class CreateSubscriptionRequest(BaseModel):
    """Request body for creating a webhook subscription."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str = Field(min_length=1, max_length=512)
    target_url: str = Field(min_length=1, max_length=2048)
    event_types: list[WebhookEventType] = Field(min_length=1, max_length=5)
    tenant_id: str = Field(default="default", min_length=1, max_length=512)

    @field_validator("event_types")
    @classmethod
    def _unique_event_types(
        cls, event_types: list[WebhookEventType]
    ) -> list[WebhookEventType]:
        if len(set(event_types)) != len(event_types):
            raise ValueError("event_types must not contain duplicates")
        return event_types


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
    run_id: str | None = None
    approval_id: str | None = None
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


class WebhookDeliveryResponse(BaseModel):
    """Safe operator view of one delivery; payload and secret are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    subscription_id: str
    event_type: str
    event_id: str
    run_id: str | None = None
    approval_id: str | None = None
    status: str
    attempt_count: int
    max_attempts: int
    last_error: str | None
    last_status_code: int | None
    created_at: str
    updated_at: str


class WebhookDeliveryListResponse(BaseModel):
    """Response for listing scoped webhook delivery state."""

    model_config = ConfigDict(extra="forbid")

    deliveries: list[WebhookDeliveryResponse]
    total: int


def _mask_secret(secret: str) -> str:
    """Return a redacted secret preview safe to include in API responses."""
    tail = secret[-4:] if len(secret) >= 4 else ""
    return f"••••{tail}"


def _event_identity(payload_json: str) -> dict[str, str | None]:
    """Extract only durable operator identity fields from a hidden event payload."""
    empty = {
        "run_id": None,
        "approval_id": None,
        "thread_id": None,
        "graph_version_ref": None,
    }
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return empty
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return empty

    def identifier(name: str) -> str | None:
        value = data.get(name)
        return value if isinstance(value, str) and 0 < len(value) <= 512 else None

    return {name: identifier(name) for name in empty}


def _event_correlation(payload_json: str) -> tuple[str | None, str | None]:
    """Compatibility projection for public run/approval response fields."""
    identity = _event_identity(payload_json)
    return identity["run_id"], identity["approval_id"]


def _serialize_subscription(
    sub: WebhookSubscription, *, reveal_secret: bool = False
) -> WebhookSubscriptionResponse:
    """Convert a WebhookSubscription to an API response.

    The raw signing secret is only ever returned on creation (`reveal_secret=True`);
    list/get responses return a masked preview so the secret cannot be re-read
    after its initial hand-off.
    """
    return WebhookSubscriptionResponse(
        subscription_id=sub.subscription_id,
        deployment_ref=sub.deployment_ref,
        tenant_id=sub.tenant_id,
        target_url=sub.target_url,
        secret=sub.secret if reveal_secret else _mask_secret(sub.secret),
        event_types=[e.value for e in sub.event_types],
        active=sub.active,
        created_at=sub.created_at.isoformat(),
        updated_at=sub.updated_at.isoformat(),
    )


def _require_permitted_target(target_url: str) -> None:
    """Refuse a subscription target that names internal infrastructure.

    A02-6: ``target_url`` is an outbound destination the caller chooses, which the
    delivery worker later POSTs to from inside the deployment's network position.
    Checked before the subscription is persisted, since the persisted row is what
    opens the socket.
    """
    try:
        validate_outbound_url(target_url, context="webhook target_url")
    except OutboundDestinationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None


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
        principal = await require_permission(request, Permission.WEBHOOK_ADMIN)
        deployment = _served_deployment(request)
        # Tenant isolation (audit F8): only the served deployment's owner may
        # subscribe, and the subscription is BOUND to that deployment + tenant.
        # The body's deployment_ref/tenant_id are NOT trusted — accepting them
        # let any tenant admin subscribe to another tenant's run events.
        await require_deployment_scope(request, deployment)
        _require_permitted_target(payload.target_url)
        webhook_service = _webhook_service(request)
        sub = WebhookSubscription(
            deployment_ref=deployment.deployment_ref,
            tenant_id=deployment.tenant_id,
            target_url=payload.target_url,
            event_types=list(payload.event_types),
        )
        _webhook_audit_recorder(request, deployment).ensure_signing_available()
        created = await webhook_service.create_subscription(sub, actor=principal.to_actor())
        return _serialize_subscription(created, reveal_secret=True)

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
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        # Scope to the served deployment's tenant; client-supplied filters are
        # ignored so one tenant cannot enumerate another's subscriptions (F8).
        subs = await webhook_service.list_subscriptions(
            deployment_ref=deployment.deployment_ref,
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
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        sub = await webhook_service.get_subscription(subscription_id)
        # Cross-scope read guard (F8): a subscription outside the served
        # deployment (foreign tenant OR foreign deployment_ref) reads as absent.
        if _foreign_subscription(sub, deployment):
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
        principal = await require_permission(request, Permission.WEBHOOK_ADMIN)
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        # Cross-scope delete guard (F8): can't deactivate a subscription outside
        # the served deployment (foreign tenant or foreign deployment_ref).
        sub = await webhook_service.get_subscription(subscription_id)
        if _foreign_subscription(sub, deployment):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="subscription not found",
            )
        _webhook_audit_recorder(request, deployment).ensure_signing_available()
        await webhook_service.deactivate_subscription(
            subscription_id, actor=principal.to_actor()
        )

    @app.get(
        "/webhooks/deliveries",
        response_model=WebhookDeliveryListResponse,
    )
    async def list_deliveries(
        request: Request,
        subscription_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> WebhookDeliveryListResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        allowed = await _served_subscription_ids(request, deployment)
        if subscription_id is not None:
            if subscription_id not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="subscription not found",
                )
            deliveries = await webhook_service.list_deliveries(
                subscription_id=subscription_id, limit=limit
            )
        else:
            deliveries = await webhook_service.list_deliveries(
                subscription_ids=sorted(allowed), limit=limit
            )
        items = [
            WebhookDeliveryResponse(
                delivery_id=item.delivery_id,
                subscription_id=item.subscription_id,
                event_type=item.event_type.value,
                event_id=item.event_id,
                run_id=_event_correlation(item.payload_json)[0],
                approval_id=_event_correlation(item.payload_json)[1],
                status=item.status.value,
                attempt_count=item.attempt_count,
                max_attempts=item.max_attempts,
                last_error=item.last_error,
                last_status_code=item.last_status_code,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
            for item in deliveries
        ]
        return WebhookDeliveryListResponse(deliveries=items, total=len(items))

    @app.get(
        "/webhooks/dead-letters",
        response_model=WebhookDeadLetterListResponse,
    )
    async def list_dead_letters(
        request: Request,
        subscription_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> WebhookDeadLetterListResponse:
        await require_permission(request, Permission.WEBHOOK_ADMIN)
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        # Scope to the served deployment's own subscriptions (F8 re-audit): the
        # dead-letters table has no tenant column, so filter by subscription set
        # IN THE QUERY — a Python post-filter after a global LIMIT would silently
        # hide the deployment's own rows behind newer foreign ones.
        allowed = await _served_subscription_ids(request, deployment)
        if subscription_id is not None:
            if subscription_id not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="subscription not found",
                )
            dead_letters = await webhook_service.list_dead_letters(
                subscription_id=subscription_id, limit=limit
            )
        else:
            dead_letters = await webhook_service.list_dead_letters(
                subscription_ids=sorted(allowed), limit=limit
            )
        items = [
            WebhookDeadLetterResponse(
                dead_letter_id=dl.dead_letter_id,
                delivery_id=dl.delivery_id,
                subscription_id=dl.subscription_id,
                event_type=dl.event_type.value,
                event_id=dl.event_id,
                run_id=_event_correlation(dl.payload_json)[0],
                approval_id=_event_correlation(dl.payload_json)[1],
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
        principal = await require_permission(request, Permission.WEBHOOK_ADMIN)
        deployment = _served_deployment(request)
        await require_deployment_scope(request, deployment)
        webhook_service = _webhook_service(request)
        # Ownership guard (F8 re-audit): the dead-letter must belong to one of the
        # served deployment's subscriptions, else a cross-tenant caller could force
        # redelivery of another tenant's event (and get an existence oracle).
        dead_letter = await webhook_service.get_dead_letter(dead_letter_id)
        allowed = await _served_subscription_ids(request, deployment)
        if dead_letter is None or dead_letter.subscription_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="dead letter not found",
            )
        identity = _event_identity(dead_letter.payload_json)
        await _webhook_audit_recorder(request, deployment).record_webhook_event(
            node_id="webhook.dead-letter.replay",
            actor=principal.to_actor(),
            subscription_id=dead_letter.subscription_id,
            delivery_id=dead_letter.delivery_id,
            event_id=dead_letter.event_id,
            dead_letter_id=dead_letter.dead_letter_id,
            event_type=dead_letter.event_type.value,
            transition="replay_authorized",
            run_id=identity["run_id"],
            approval_id=identity["approval_id"],
            thread_id=identity["thread_id"],
            graph_version_ref=identity["graph_version_ref"],
        )
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


def _served_deployment(request: Request) -> Any:
    """Return this service's own (single) served deployment for tenant scoping."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    deployment = getattr(bootstrap, "deployment", None) if bootstrap is not None else None
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service bootstrap is not configured",
        )
    return deployment


def _webhook_audit_recorder(request: Request, deployment: Any) -> ServiceAuditRecorder:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    audit_repository = (
        getattr(bootstrap, "audit_repository", None) if bootstrap is not None else None
    )
    if audit_repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook audit repository is unavailable",
        )
    return ServiceAuditRecorder(
        repository=audit_repository,
        deployment=deployment,
        require_signed=True,
    )


async def _served_subscription_ids(request: Request, deployment: Any) -> set[str]:
    """Subscription ids owned by the served deployment.

    The ``webhook_dead_letters`` table carries no tenant/deployment column, so
    dead-letter routes are scoped via the deployment's own subscriptions rather
    than by a row predicate — ``require_deployment_scope`` only gates the caller,
    not which rows return (audit F8 re-audit).
    """
    webhook_service = _webhook_service(request)
    subs = await webhook_service.list_subscriptions(
        deployment_ref=deployment.deployment_ref,
    )
    return {s.subscription_id for s in subs}


def _foreign_subscription(sub: Any, deployment: Any) -> bool:
    """Whether a subscription is outside the served deployment.

    True when absent, foreign tenant, or foreign deployment_ref — so the by-id
    routes treat it as not-found, matching the list route's scope (F8 re-audit).
    """
    return (
        sub is None
        or sub.tenant_id != deployment.tenant_id
        or sub.deployment_ref != deployment.deployment_ref
    )
