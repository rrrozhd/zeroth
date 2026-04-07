"""Webhook event emission and subscription management service.

Provides the business-logic layer between REST endpoints and the repository.
Responsible for matching events to subscriptions and enqueuing deliveries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from zeroth.webhooks.models import (
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.webhooks.repository import WebhookRepository

logger = logging.getLogger(__name__)


@dataclass
class WebhookService:
    """High-level webhook operations: emit events, manage subscriptions, replay dead-letters."""

    repository: WebhookRepository
    default_max_retries: int = 5

    async def emit_event(
        self,
        *,
        event_type: WebhookEventType | str,
        deployment_ref: str,
        tenant_id: str,
        data: dict,
    ) -> list[WebhookDelivery]:
        """Find active subscriptions matching deployment_ref + event_type, enqueue delivery each."""
        event_type = WebhookEventType(event_type)
        subs = await self.repository.list_subscriptions_for_event(deployment_ref, event_type)
        payload = WebhookEventPayload(
            event_type=event_type,
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
            data=data,
        )
        payload_json = payload.model_dump_json()
        deliveries: list[WebhookDelivery] = []
        for sub in subs:
            delivery = WebhookDelivery(
                subscription_id=sub.subscription_id,
                event_type=event_type,
                event_id=payload.event_id,
                payload_json=payload_json,
                max_attempts=self.default_max_retries,
            )
            deliveries.append(await self.repository.enqueue_delivery(delivery))
        return deliveries

    async def create_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        """Persist a new webhook subscription."""
        return await self.repository.create_subscription(sub)

    async def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        """Look up a subscription by ID."""
        return await self.repository.get_subscription(subscription_id)

    async def list_subscriptions(
        self,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> list[WebhookSubscription]:
        """List subscriptions, optionally filtered."""
        return await self.repository.list_subscriptions(
            deployment_ref=deployment_ref, tenant_id=tenant_id
        )

    async def deactivate_subscription(self, subscription_id: str) -> None:
        """Soft-delete a subscription by marking it inactive."""
        await self.repository.deactivate_subscription(subscription_id)

    async def delete_subscription(self, subscription_id: str) -> None:
        """Hard-delete a subscription."""
        await self.repository.delete_subscription(subscription_id)

    async def replay_dead_letter(self, dead_letter_id: str) -> WebhookDelivery:
        """Re-enqueue a dead-letter entry as a new pending delivery."""
        dl = await self.repository.get_dead_letter(dead_letter_id)
        if dl is None:
            raise KeyError(dead_letter_id)
        delivery = WebhookDelivery(
            subscription_id=dl.subscription_id,
            event_type=dl.event_type,
            event_id=dl.event_id,
            payload_json=dl.payload_json,
            max_attempts=self.default_max_retries,
        )
        return await self.repository.enqueue_delivery(delivery)

    async def list_dead_letters(
        self,
        subscription_id: str | None = None,
        limit: int = 50,
    ) -> list[WebhookDeadLetter]:
        """List dead-letter entries."""
        return await self.repository.list_dead_letters(
            subscription_id=subscription_id, limit=limit
        )
