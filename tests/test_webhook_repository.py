"""Tests for WebhookRepository and Alembic migration 003."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import zeroth.service.webhooks.repository as webhook_repository
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.service.webhooks.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from tests.conftest import requires_docker


@pytest.mark.asyncio
async def test_stale_delivery_lease_cannot_overwrite_reclaimed_success(
    async_database, make_subscription, make_delivery, monkeypatch
) -> None:
    from zeroth.service.webhooks.repository import WebhookRepository

    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(webhook_repository, "utc_now", lambda: now)
    repo = WebhookRepository.for_default_compatibility(async_database)
    sub = make_subscription()
    await repo.create_subscription(sub)
    delivery = make_delivery(
        subscription_id=sub.subscription_id,
        next_attempt_at=now - timedelta(seconds=1),
    )
    await repo.enqueue_delivery(delivery)
    claim_a = await repo.claim_pending_delivery(lease_seconds=1)
    assert claim_a is not None

    monkeypatch.setattr(webhook_repository, "utc_now", lambda: now + timedelta(seconds=2))
    claim_b = await repo.claim_pending_delivery(lease_seconds=30)
    assert claim_b is not None and claim_b.generation > claim_a.generation
    assert await repo.mark_delivered(delivery.delivery_id, claim_b.generation)
    assert not await repo.mark_failed(
        delivery.delivery_id,
        claim_a.generation,
        error="stale",
        status_code=500,
        retry_delay=0,
    )
    assert not await repo.dead_letter(delivery.delivery_id, claim_a.generation)

    async with async_database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery.delivery_id,),
        )
    assert row["status"] == DeliveryStatus.DELIVERED.value
    assert await repo.list_dead_letters() == []


@pytest.mark.asyncio
async def test_repeated_dead_letter_transition_is_idempotent(
    async_database, make_subscription, make_delivery
) -> None:
    from zeroth.service.webhooks.repository import WebhookRepository

    repo = WebhookRepository.for_default_compatibility(async_database)
    sub = make_subscription()
    await repo.create_subscription(sub)
    delivery = make_delivery(
        subscription_id=sub.subscription_id,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await repo.enqueue_delivery(delivery)
    claim = await repo.claim_pending_delivery()
    assert claim is not None

    assert await repo.dead_letter(delivery.delivery_id, claim.generation)
    assert not await repo.dead_letter(delivery.delivery_id, claim.generation)
    rows = await repo.list_dead_letters()
    assert len(rows) == 1
    assert rows[0].delivery_id == delivery.delivery_id


async def test_claim_pushes_eligibility_and_batch_limit_to_scoped_query(
    async_database, make_subscription, make_delivery, monkeypatch
) -> None:
    from zeroth.service.webhooks.repository import WebhookRepository

    repository = WebhookRepository.for_default_compatibility(async_database)
    subscription = make_subscription()
    await repository.create_subscription(subscription)
    await repository.enqueue_delivery(
        make_delivery(
            subscription_id=subscription.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    calls: list[dict[str, object]] = []
    original = BoundStructuredTable.select

    async def recording_select(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BoundStructuredTable, "select", recording_select)

    assert await repository.claim_pending_delivery() is not None
    assert calls[0]["where_in"] == {
        "status": ("pending", "failed", "delivering")
    }
    assert calls[0]["order_by"] == ("next_attempt_at",)
    assert calls[0]["limit"] == 16


async def test_dead_letter_list_pushes_filters_order_and_limit_to_scoped_query(
    async_database, make_subscription, make_delivery, monkeypatch
) -> None:
    from zeroth.service.webhooks.repository import WebhookRepository

    repository = WebhookRepository.for_default_compatibility(async_database)
    subscription = make_subscription()
    await repository.create_subscription(subscription)
    delivery = make_delivery(
        subscription_id=subscription.subscription_id,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await repository.enqueue_delivery(delivery)
    claim = await repository.claim_pending_delivery()
    assert claim is not None
    await repository.dead_letter(delivery.delivery_id, claim.generation)
    calls: list[dict[str, object]] = []
    original = BoundStructuredTable.select

    async def recording_select(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BoundStructuredTable, "select", recording_select)

    rows = await repository.list_dead_letters(
        subscription_ids=[subscription.subscription_id], limit=1
    )

    assert len(rows) == 1
    assert calls == [
        {
            "where": None,
            "where_in": {"subscription_id": (subscription.subscription_id,)},
            "order_by_desc": ("dead_lettered_at",),
            "limit": 1,
        }
    ]


@requires_docker
@pytest.mark.asyncio
async def test_postgres_concurrent_claim_returns_delivery_exactly_once(
    postgres_database, make_subscription, make_delivery
) -> None:
    from zeroth.service.webhooks.repository import WebhookRepository

    repo_a = WebhookRepository.for_default_compatibility(postgres_database)
    repo_b = WebhookRepository.for_default_compatibility(postgres_database)
    sub = make_subscription()
    await repo_a.create_subscription(sub)
    await repo_a.enqueue_delivery(
        make_delivery(
            subscription_id=sub.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    claims = await asyncio.gather(repo_a.claim_pending_delivery(), repo_b.claim_pending_delivery())

    assert sum(claim is not None for claim in claims) == 1


def test_webhook_repository_constructor_requires_scope_context() -> None:
    import inspect

    from zeroth.service.webhooks.repository import WebhookRepository

    parameters = inspect.signature(WebhookRepository).parameters
    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_webhook_subscription_collision_preserves_each_tenant_owner(
    async_database, make_subscription
):
    from zeroth.service.webhooks.repository import WebhookRepository

    tenant_a = WebhookRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-a"))
    tenant_b = WebhookRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-b"))
    await tenant_a.create_subscription(
        make_subscription(subscription_id="shared-sub", tenant_id="tenant-a")
    )
    await tenant_b.create_subscription(
        make_subscription(subscription_id="shared-sub", tenant_id="tenant-b")
    )

    assert (await tenant_a.get_subscription("shared-sub")).tenant_id == "tenant-a"
    assert (await tenant_b.get_subscription("shared-sub")).tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_foreign_webhook_read_and_list_match_unknown_scope(async_database, make_subscription):
    from zeroth.service.webhooks.repository import WebhookRepository

    owner = WebhookRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-a"))
    foreign = WebhookRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-b"))
    await owner.create_subscription(
        make_subscription(subscription_id="owner-sub", tenant_id="tenant-a")
    )

    assert await foreign.get_subscription("owner-sub") is None
    assert await foreign.get_subscription("unknown-sub") is None
    assert await foreign.list_subscriptions() == []


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
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        # Should not raise - table exists
        result = await repo.list_subscriptions()
        assert result == []

    @pytest.mark.asyncio
    async def test_webhook_deliveries_table_exists(self, async_database):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        result = await repo.claim_pending_delivery()
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_dead_letters_table_exists(self, async_database):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
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
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        result = await repo.create_subscription(sub)
        assert result.subscription_id == sub.subscription_id
        assert result.deployment_ref == "deploy-test"

    @pytest.mark.asyncio
    async def test_get_subscription_exists(self, async_database, make_subscription):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        fetched = await repo.get_subscription(sub.subscription_id)
        assert fetched is not None
        assert fetched.subscription_id == sub.subscription_id

    @pytest.mark.asyncio
    async def test_get_subscription_missing(self, async_database):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        assert await repo.get_subscription("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_subscriptions_for_event(self, async_database, make_subscription):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub1 = make_subscription(
            event_types=[WebhookEventType.RUN_COMPLETED, WebhookEventType.RUN_FAILED]
        )
        sub2 = make_subscription(event_types=[WebhookEventType.APPROVAL_REQUESTED])
        sub3 = make_subscription(
            deployment_ref="other-deploy", event_types=[WebhookEventType.RUN_COMPLETED]
        )
        await repo.create_subscription(sub1)
        await repo.create_subscription(sub2)
        await repo.create_subscription(sub3)

        matches = await repo.list_subscriptions_for_event(
            "deploy-test", WebhookEventType.RUN_COMPLETED
        )
        ids = [s.subscription_id for s in matches]
        assert sub1.subscription_id in ids
        assert sub2.subscription_id not in ids
        assert sub3.subscription_id not in ids  # different deployment

    @pytest.mark.asyncio
    async def test_deactivate_subscription(self, async_database, make_subscription, monkeypatch):
        from zeroth.service.webhooks.repository import WebhookRepository

        fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(webhook_repository, "utc_now", lambda: fixed)
        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        await repo.deactivate_subscription(sub.subscription_id)
        fetched = await repo.get_subscription(sub.subscription_id)
        assert fetched is not None
        assert fetched.active is False
        assert fetched.updated_at == fixed

    @pytest.mark.asyncio
    async def test_deactivated_excluded_from_event_list(self, async_database, make_subscription):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        await repo.deactivate_subscription(sub.subscription_id)
        matches = await repo.list_subscriptions_for_event(
            "deploy-test", WebhookEventType.RUN_COMPLETED
        )
        assert len(matches) == 0


class TestWebhookRepositoryDeliveries:
    """Delivery lifecycle operations."""

    @pytest.mark.asyncio
    async def test_enqueue_delivery(self, async_database, make_subscription, make_delivery):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        result = await repo.enqueue_delivery(delivery)
        assert result.delivery_id == delivery.delivery_id
        assert result.status == DeliveryStatus.PENDING

    @pytest.mark.asyncio
    async def test_claim_pending_delivery(self, async_database, make_subscription, make_delivery):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(
            subscription_id=sub.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await repo.enqueue_delivery(delivery)
        claimed = await repo.claim_pending_delivery()
        assert claimed is not None
        assert claimed.delivery.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_empty(self, async_database):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        assert await repo.claim_pending_delivery() is None

    @pytest.mark.asyncio
    async def test_failed_delivery_is_reclaimed_for_retry(
        self, async_database, make_subscription, make_delivery
    ):
        """A FAILED delivery whose backoff has elapsed must be re-claimed.

        Regression: claim_pending_delivery filtered status=PENDING only while
        mark_failed sets status=FAILED, so a once-failed delivery was never
        retried -- the backoff/next_attempt_at machinery was dead code.
        """
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(
            subscription_id=sub.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await repo.enqueue_delivery(delivery)

        first = await repo.claim_pending_delivery()
        assert first is not None and first.delivery.delivery_id == delivery.delivery_id

        # Worker reports failure; retry_delay=0 => the retry is due immediately.
        await repo.mark_failed(
            delivery.delivery_id,
            first.generation,
            error="HTTP 500",
            status_code=500,
            retry_delay=0.0,
        )

        retry = await repo.claim_pending_delivery()
        assert retry is not None, "failed delivery was never re-claimed for retry"
        assert retry.delivery.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_claimed_delivery_is_not_double_claimed(
        self, async_database, make_subscription, make_delivery
    ):
        """Claiming leases the row so a second claim can't grab the same delivery.

        Regression: claim did not mark the row in-flight, so the polling worker
        re-claimed the still-PENDING row and delivered it multiple times.
        """
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(
            subscription_id=sub.subscription_id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await repo.enqueue_delivery(delivery)

        first = await repo.claim_pending_delivery()
        assert first is not None and first.delivery.delivery_id == delivery.delivery_id

        second = await repo.claim_pending_delivery()
        assert second is None, "same delivery claimed twice (would double-deliver)"

    @pytest.mark.asyncio
    async def test_expired_delivering_lease_is_reclaimed(
        self, async_database, make_subscription, make_delivery
    ):
        """A DELIVERING row whose lease expired is reclaimed (worker-crash recovery)."""
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(
            subscription_id=sub.subscription_id,
            status=DeliveryStatus.DELIVERING,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        await repo.enqueue_delivery(delivery)

        claimed = await repo.claim_pending_delivery()
        assert claimed is not None, "expired DELIVERING lease was not reclaimed"
        assert claimed.delivery.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_mark_delivered(self, async_database, make_subscription, make_delivery):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        claim = await repo.claim_pending_delivery()
        assert claim is not None
        await repo.mark_delivered(delivery.delivery_id, claim.generation)

        # Verify status updated
        async with async_database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            )
        assert row["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_mark_failed_increments_attempt_count(
        self, async_database, make_subscription, make_delivery
    ):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        claim = await repo.claim_pending_delivery()
        assert claim is not None
        await repo.mark_failed(
            delivery.delivery_id,
            claim.generation,
            error="timeout",
            status_code=504,
            retry_delay=1.0,
        )

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
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)

        claim = await repo.claim_pending_delivery()
        assert claim is not None
        await repo.dead_letter(delivery.delivery_id, claim.generation)

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
    async def test_list_dead_letters_ordered_desc(
        self, async_database, make_subscription, make_delivery
    ):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)

        # Create two deliveries and dead-letter them
        d1 = make_delivery(subscription_id=sub.subscription_id)
        d2 = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(d1)
        await repo.enqueue_delivery(d2)
        claim1 = await repo.claim_pending_delivery()
        assert claim1 is not None
        await repo.dead_letter(claim1.delivery.delivery_id, claim1.generation)
        claim2 = await repo.claim_pending_delivery()
        assert claim2 is not None
        await repo.dead_letter(claim2.delivery.delivery_id, claim2.generation)

        dls = await repo.list_dead_letters(subscription_id=sub.subscription_id)
        assert len(dls) == 2
        # Most recent dead-lettered first
        assert dls[0].dead_lettered_at >= dls[1].dead_lettered_at

    @pytest.mark.asyncio
    async def test_get_dead_letter(self, async_database, make_subscription, make_delivery):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        sub = make_subscription()
        await repo.create_subscription(sub)
        delivery = make_delivery(subscription_id=sub.subscription_id)
        await repo.enqueue_delivery(delivery)
        claim = await repo.claim_pending_delivery()
        assert claim is not None
        await repo.dead_letter(delivery.delivery_id, claim.generation)

        dls = await repo.list_dead_letters(subscription_id=sub.subscription_id)
        assert len(dls) == 1
        dl = await repo.get_dead_letter(dls[0].dead_letter_id)
        assert dl is not None
        assert dl.delivery_id == delivery.delivery_id

    @pytest.mark.asyncio
    async def test_get_dead_letter_missing(self, async_database):
        from zeroth.service.webhooks.repository import WebhookRepository

        repo = WebhookRepository.for_default_compatibility(async_database)
        assert await repo.get_dead_letter("nonexistent") is None
