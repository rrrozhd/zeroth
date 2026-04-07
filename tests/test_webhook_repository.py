"""Tests for WebhookRepository and Alembic migration 003."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zeroth.webhooks.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)


@pytest.fixture
def make_subscription() -> callable:
    """Factory for WebhookSubscription with defaults."""

    def _make(**overrides) -> WebhookSubscription:
        defaults = {
            "deployment_ref": "deploy-test",
            "target_url": "https://example.com/webhook",
            "event_types": [WebhookEventType.RUN_COMPLETED],
        }
        defaults.update(overrides)
        return WebhookSubscription(**defaults)

    return _make


@pytest.fixture
def make_delivery() -> callable:
    """Factory for WebhookDelivery with defaults."""

    def _make(**overrides) -> WebhookDelivery:
        defaults = {
            "subscription_id": "sub-test",
            "event_type": WebhookEventType.RUN_COMPLETED,
            "payload_json": '{"test": true}',
        }
        defaults.update(overrides)
        return WebhookDelivery(**defaults)

    return _make


class TestMigration003:
    """Verify migration 003 creates webhook tables and SLA columns."""

    @pytest.mark.asyncio
    async def test_webhook_subscriptions_table_exists(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        # Should not raise - table exists
        result = await repo.list_subscriptions()
        assert result == []

    @pytest.mark.asyncio
    async def test_webhook_deliveries_table_exists(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        result = await repo.claim_pending_delivery()
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_dead_letters_table_exists(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        result = await repo.list_dead_letters()
        assert result == []

    @pytest.mark.asyncio
    async def test_approvals_sla_columns_exist(self, async_database):
        """Verify sla_deadline, escalation_action, escalated_from_id columns added to approvals."""
        async with async_database.transaction() as conn:
            # Should not raise - columns exist (nullable)
            await conn.execute(
                "INSERT INTO approvals (approval_id, run_id, node_id, graph_version_ref, "
                "deployment_ref, status, created_at, updated_at, record_json, "
                "sla_deadline, escalation_action, escalated_from_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "test-ap",
                    "run-1",
                    "node-1",
                    "gv-1",
                    "dep-1",
                    "pending",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                    "{}",
                    None,
                    None,
                    None,
                ),
            )
            row = await conn.fetch_one(
                "SELECT sla_deadline, escalation_action, escalated_from_id FROM approvals WHERE approval_id = ?",
                ("test-ap",),
            )
        assert row is not None
        assert row["sla_deadline"] is None
        assert row["escalation_action"] is None
        assert row["escalated_from_id"] is None


class TestWebhookRepositorySubscriptions:
    """Subscription CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_subscription(self, async_database, make_subscription):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        result = await repo.create_subscription(sub)
        assert result.subscription_id == sub.subscription_id
        assert result.deployment_ref == "deploy-test"

    @pytest.mark.asyncio
    async def test_get_subscription_exists(self, async_database, make_subscription):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        fetched = await repo.get_subscription(sub.subscription_id)
        assert fetched is not None
        assert fetched.subscription_id == sub.subscription_id

    @pytest.mark.asyncio
    async def test_get_subscription_missing(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        assert await repo.get_subscription("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_subscriptions_for_event(self, async_database, make_subscription):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub1 = make_subscription(
            event_types=[WebhookEventType.RUN_COMPLETED, WebhookEventType.RUN_FAILED]
        )
        sub2 = make_subscription(event_types=[WebhookEventType.APPROVAL_REQUESTED])
        sub3 = make_subscription(deployment_ref="other-deploy", event_types=[WebhookEventType.RUN_COMPLETED])
        await repo.create_subscription(sub1)
        await repo.create_subscription(sub2)
        await repo.create_subscription(sub3)

        matches = await repo.list_subscriptions_for_event("deploy-test", WebhookEventType.RUN_COMPLETED)
        ids = [s.subscription_id for s in matches]
        assert sub1.subscription_id in ids
        assert sub2.subscription_id not in ids
        assert sub3.subscription_id not in ids  # different deployment

    @pytest.mark.asyncio
    async def test_deactivate_subscription(self, async_database, make_subscription):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        await repo.deactivate_subscription(sub.subscription_id)
        fetched = await repo.get_subscription(sub.subscription_id)
        assert fetched is not None
        assert fetched.active is False

    @pytest.mark.asyncio
    async def test_deactivated_excluded_from_event_list(self, async_database, make_subscription):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        await repo.deactivate_subscription(sub.subscription_id)
        matches = await repo.list_subscriptions_for_event("deploy-test", WebhookEventType.RUN_COMPLETED)
        assert len(matches) == 0


class TestWebhookRepositoryDeliveries:
    """Delivery lifecycle operations."""

    @pytest.mark.asyncio
    async def test_enqueue_delivery(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        result = await repo.enqueue_delivery(delivery)
        assert result.delivery_id == delivery.delivery_id
        assert result.status == DeliveryStatus.PENDING

    @pytest.mark.asyncio
    async def test_claim_pending_delivery(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(
            subscription_id=sub.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await repo.enqueue_delivery(delivery)
        claimed = await repo.claim_pending_delivery()
        assert claimed is not None
        assert claimed.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_empty(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        assert await repo.claim_pending_delivery() is None

    @pytest.mark.asyncio
    async def test_mark_delivered(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        await repo.mark_delivered(delivery.delivery_id)

        # Verify status updated
        async with async_database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            )
        assert row["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_mark_failed_increments_attempt_count(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        await repo.mark_failed(delivery.delivery_id, error="timeout", status_code=504, retry_delay=1.0)

        async with async_database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT attempt_count, status, last_error, last_status_code FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            )
        assert row["attempt_count"] == 1
        assert row["status"] == "failed"
        assert row["last_error"] == "timeout"
        assert row["last_status_code"] == 504

    @pytest.mark.asyncio
    async def test_dead_letter(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)

        # Simulate some failures first
        await repo.mark_failed(delivery.delivery_id, error="err", status_code=500, retry_delay=1.0)

        await repo.dead_letter(delivery.delivery_id)

        # Delivery status should be dead_letter
        async with async_database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            )
        assert row["status"] == "dead_letter"

        # Dead letter entry should exist
        dls = await repo.list_dead_letters(subscription_id=sub.subscription_id)
        assert len(dls) == 1
        assert dls[0].delivery_id == delivery.delivery_id


class TestWebhookRepositoryDeadLetters:
    """Dead-letter query operations."""

    @pytest.mark.asyncio
    async def test_list_dead_letters_ordered_desc(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)

        # Create two deliveries and dead-letter them
        d1 = make_delivery(subscription_id=sub.subscription_id)
        d2 = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(d1)
        await repo.enqueue_delivery(d2)
        await repo.dead_letter(d1.delivery_id)
        await repo.dead_letter(d2.delivery_id)

        dls = await repo.list_dead_letters(subscription_id=sub.subscription_id)
        assert len(dls) == 2
        # Most recent dead-lettered first
        assert dls[0].dead_lettered_at >= dls[1].dead_lettered_at

    @pytest.mark.asyncio
    async def test_get_dead_letter(self, async_database, make_subscription, make_delivery):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        await repo.dead_letter(delivery.delivery_id)

        dls = await repo.list_dead_letters(subscription_id=sub.subscription_id)
        assert len(dls) == 1
        dl = await repo.get_dead_letter(dls[0].dead_letter_id)
        assert dl is not None
        assert dl.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_get_dead_letter_missing(self, async_database):
        from zeroth.webhooks.repository import WebhookRepository

        repo = WebhookRepository(async_database)
        assert await repo.get_dead_letter("nonexistent") is None
