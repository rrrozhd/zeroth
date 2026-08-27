"""Webhook event emission and subscription management service.

Provides the business-logic layer between REST endpoints and the repository.
Responsible for matching events to subscriptions and enqueuing deliveries.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from zeroth.governance.identity import ActorIdentity
from zeroth.service.service_audit import ServiceAuditRecorder, webhook_event_identity
from zeroth.service.webhooks.models import (
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository

logger = logging.getLogger(__name__)


@dataclass
class WebhookService:
    """High-level webhook operations: emit events, manage subscriptions, replay dead-letters."""

    repository: WebhookRepository
    default_max_retries: int = 5
    audit_recorder: ServiceAuditRecorder | None = None

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
            deliveries.append(
                WebhookDelivery(
                    subscription_id=sub.subscription_id,
                    event_type=event_type,
                    event_id=payload.event_id,
                    payload_json=payload_json,
                    max_attempts=self.default_max_retries,
                )
            )
        if not deliveries:
            return []
        if self.audit_recorder is None:
            return await self.repository.enqueue_deliveries(deliveries)
        records = [self._build_enqueue_audit(delivery) for delivery in deliveries]
        return await self.repository.enqueue_deliveries(
            deliveries,
            audit_records=records,
            audit_repository=self.audit_recorder.repository,
        )

    async def create_subscription(
        self,
        sub: WebhookSubscription,
        *,
        actor: ActorIdentity | None = None,
    ) -> WebhookSubscription:
        """Persist a new webhook subscription."""
        if self.audit_recorder is None:
            return await self.repository.create_subscription(sub)
        record = self.audit_recorder.build_webhook_event(
            node_id="webhook.subscription.create",
            actor=actor,
            subscription_id=sub.subscription_id,
            transition="subscription_created",
            target_url=sub.target_url,
        )
        return await self.repository.create_subscription(
            sub,
            audit_record=record,
            audit_repository=self.audit_recorder.repository,
        )

    async def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        """Look up a subscription by ID."""
        return await self.repository.get_subscription(subscription_id)

    async def list_subscriptions(
        self,
        deployment_ref: str | None = None,
    ) -> list[WebhookSubscription]:
        """List subscriptions, optionally filtered."""
        return await self.repository.list_subscriptions(deployment_ref=deployment_ref)

    async def list_deliveries(
        self,
        subscription_id: str | None = None,
        limit: int = 50,
        subscription_ids: Sequence[str] | None = None,
    ) -> list[WebhookDelivery]:
        """List delivery state without exposing payloads or signing material."""
        return await self.repository.list_deliveries(
            subscription_id=subscription_id,
            subscription_ids=subscription_ids,
            limit=limit,
        )

    async def deactivate_subscription(
        self,
        subscription_id: str,
        *,
        actor: ActorIdentity | None = None,
    ) -> None:
        """Soft-delete a subscription by marking it inactive."""
        if self.audit_recorder is None:
            await self.repository.deactivate_subscription(subscription_id)
            return
        sub = await self.repository.get_subscription(subscription_id)
        if sub is None:
            raise KeyError(subscription_id)
        record = self.audit_recorder.build_webhook_event(
            node_id="webhook.subscription.deactivate",
            actor=actor,
            subscription_id=subscription_id,
            transition="subscription_deactivated",
            target_url=sub.target_url,
        )
        await self.repository.deactivate_subscription(
            subscription_id,
            audit_record=record,
            audit_repository=self.audit_recorder.repository,
        )

    async def delete_subscription(self, subscription_id: str) -> None:
        """Hard-delete a subscription."""
        await self.repository.delete_subscription(subscription_id)

    async def get_dead_letter(self, dead_letter_id: str) -> WebhookDeadLetter | None:
        """Look up a single dead-letter entry by ID (for ownership scoping)."""
        return await self.repository.get_dead_letter(dead_letter_id)

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
        if self.audit_recorder is None:
            return (await self.repository.enqueue_deliveries([delivery]))[0]
        record = self._build_enqueue_audit(delivery)
        return (
            await self.repository.enqueue_deliveries(
                [delivery],
                audit_records=[record],
                audit_repository=self.audit_recorder.repository,
            )
        )[0]

    async def list_dead_letters(
        self,
        subscription_id: str | None = None,
        limit: int = 50,
        subscription_ids: Sequence[str] | None = None,
    ) -> list[WebhookDeadLetter]:
        """List dead-letter entries."""
        return await self.repository.list_dead_letters(
            subscription_id=subscription_id, limit=limit, subscription_ids=subscription_ids
        )

    def _build_enqueue_audit(self, delivery: WebhookDelivery):
        """Build the signed-audit input before opening the state transaction."""
        if self.audit_recorder is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("webhook audit recorder is unavailable")
        identity = webhook_event_identity(delivery.payload_json)
        return self.audit_recorder.build_webhook_event(
            node_id="webhook.delivery.enqueue",
            actor=None,
            subscription_id=delivery.subscription_id,
            delivery_id=delivery.delivery_id,
            event_id=delivery.event_id,
            event_type=delivery.event_type.value,
            transition="delivery_enqueued",
            run_id=identity["run_id"],
            approval_id=identity["approval_id"],
            thread_id=identity["thread_id"],
            graph_version_ref=identity["graph_version_ref"],
        )


WebhookService.__signature__ = inspect.signature(WebhookService).replace(  # type: ignore[attr-defined]
    parameters=[
        parameter
        for name, parameter in inspect.signature(WebhookService).parameters.items()
        if name != "audit_recorder"
    ]
)
