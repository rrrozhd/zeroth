"""Tests for WebhookService: event emission, subscription management, dead-letter replay."""

from __future__ import annotations

import json

import pytest

from zeroth.core.webhooks.models import (
    DeliveryStatus,
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.core.webhooks.repository import WebhookRepository
from zeroth.core.webhooks.service import WebhookService


@pytest.fixture
async def webhook_repo(sqlite_db):
    return WebhookRepository(sqlite_db)


@pytest.fixture
async def webhook_service(webhook_repo):
    return WebhookService(repository=webhook_repo, default_max_retries=5)


async def _create_sub(
    webhook_repo: WebhookRepository,
    *,
    deployment_ref: str = "deploy-1",
    tenant_id: str = "default",
    event_types: list[WebhookEventType] | None = None,
    active: bool = True,
) -> WebhookSubscription:
    sub = WebhookSubscription(
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        target_url="https://example.com/hook",
        event_types=event_types or [WebhookEventType.RUN_COMPLETED],
        active=active,
    )
    return await webhook_repo.create_subscription(sub)


class TestEmitEvent:
    """WebhookService.emit_event enqueues deliveries for matching subscriptions."""

    async def test_enqueues_delivery_per_matching_subscription(self, webhook_service, webhook_repo):
        sub1 = await _create_sub(webhook_repo, deployment_ref="deploy-1")
        sub2 = await _create_sub(
            webhook_repo,
            deployment_ref="deploy-1",
            event_types=[WebhookEventType.RUN_COMPLETED, WebhookEventType.RUN_FAILED],
        )
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1"},
        )
        assert len(deliveries) == 2
        sub_ids = {d.subscription_id for d in deliveries}
        assert sub_ids == {sub1.subscription_id, sub2.subscription_id}
        for d in deliveries:
            assert d.status == DeliveryStatus.PENDING
            assert d.event_type == WebhookEventType.RUN_COMPLETED

    async def test_no_matching_subscriptions_enqueues_nothing(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="other-deploy")
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1"},
        )
        assert deliveries == []

    async def test_payload_structure(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo)
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1", "status": "completed"},
        )
        assert len(deliveries) == 1
        payload = WebhookEventPayload.model_validate_json(deliveries[0].payload_json)
        assert payload.event_type == WebhookEventType.RUN_COMPLETED
        assert payload.deployment_ref == "deploy-1"
        assert payload.tenant_id == "default"
        assert payload.data == {"run_id": "r1", "status": "completed"}
        assert payload.event_id  # non-empty

    async def test_inactive_subscriptions_excluded(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, active=False)
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={},
        )
        assert deliveries == []


class TestSubscriptionManagement:
    """WebhookService delegates subscription CRUD to repository."""

    async def test_create_subscription(self, webhook_service):
        sub = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        result = await webhook_service.create_subscription(sub)
        assert result.subscription_id == sub.subscription_id

    async def test_list_subscriptions(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="deploy-1")
        await _create_sub(webhook_repo, deployment_ref="deploy-2")
        result = await webhook_service.list_subscriptions()
        assert len(result) == 2

    async def test_list_subscriptions_filtered(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="deploy-1")
        await _create_sub(webhook_repo, deployment_ref="deploy-2")
        result = await webhook_service.list_subscriptions(deployment_ref="deploy-1")
        assert len(result) == 1
        assert result[0].deployment_ref == "deploy-1"

    async def test_deactivate_subscription(self, webhook_service, webhook_repo):
        sub = await _create_sub(webhook_repo)
        await webhook_service.deactivate_subscription(sub.subscription_id)
        updated = await webhook_repo.get_subscription(sub.subscription_id)
        assert updated is not None
        assert updated.active is False


class TestReplayDeadLetter:
    """WebhookService.replay_dead_letter re-enqueues a dead-letter entry."""

    async def test_replay_creates_new_pending_delivery(self, webhook_service, webhook_repo):
        sub = await _create_sub(webhook_repo)
        # Create a delivery and dead-letter it
        delivery = WebhookDelivery(
            subscription_id=sub.subscription_id,
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="evt-1",
            payload_json='{"test": true}',
            max_attempts=1,
            attempt_count=1,
        )
        delivery = await webhook_repo.enqueue_delivery(delivery)
        await webhook_repo.dead_letter(delivery.delivery_id)

        dead_letters = await webhook_repo.list_dead_letters()
        assert len(dead_letters) == 1
        dl = dead_letters[0]

        replayed = await webhook_service.replay_dead_letter(dl.dead_letter_id)
        assert replayed.status == DeliveryStatus.PENDING
        assert replayed.subscription_id == sub.subscription_id
        assert replayed.event_type == WebhookEventType.RUN_COMPLETED
        assert replayed.payload_json == '{"test": true}'
        assert replayed.attempt_count == 0

    async def test_replay_nonexistent_raises_key_error(self, webhook_service):
        with pytest.raises(KeyError):
            await webhook_service.replay_dead_letter("nonexistent-id")
