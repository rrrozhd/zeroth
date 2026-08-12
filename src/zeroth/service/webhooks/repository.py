"""Async database layer for webhook subscriptions, deliveries, and dead-letters.

Uses the AsyncDatabase protocol for storage. Follows the same patterns as
ApprovalRepository: ?-placeholder SQL, to_json_value/load_typed_value for
JSON columns, async with self._database.transaction() as connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.service.webhooks.models import (
    DeliveryStatus,
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)


def _new_id() -> str:
    """Generate a new unique ID string."""
    return uuid4().hex


class WebhookRepository:
    """Saves and loads webhook subscriptions, deliveries, and dead-letter entries.

    Provides full CRUD for subscriptions, delivery lifecycle management
    (enqueue, claim, mark delivered/failed, dead-letter), and dead-letter queries.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext,
    ) -> None:
        if type(scope_context) not in {ScopeContext, NullWorkspaceScopeContext}:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._database = database
        self._scope_context = scope_context
        self._subscriptions = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.webhook_subscriptions",
            scope_context,
        )
        self._deliveries = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.webhook_deliveries", scope_context
        )
        self._dead_letters = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.webhook_dead_letters", scope_context
        )

    @classmethod
    def for_default_compatibility(cls, database: AsyncDatabase) -> WebhookRepository:
        return cls(database, NullWorkspaceScopeContext.for_default_compatibility())

    # ── Subscription CRUD ──────────────────────────────────────────────

    async def create_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        """Persist a new webhook subscription and return it."""
        if sub.tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        await self._subscriptions.insert(
            {
                "subscription_id": sub.subscription_id,
                "deployment_ref": sub.deployment_ref,
                "target_url": sub.target_url,
                "secret": sub.secret,
                "event_types": to_json_value(list(sub.event_types)),
                "active": 1 if sub.active else 0,
                "created_at": sub.created_at.isoformat(),
                "updated_at": sub.updated_at.isoformat(),
            }
        )
        return sub

    async def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        """Look up a subscription by ID. Returns None if not found."""
        row = await self._subscriptions.select_one(where={"subscription_id": subscription_id})
        if row is None:
            return None
        return self._row_to_subscription(row)

    async def list_subscriptions(
        self,
        deployment_ref: str | None = None,
    ) -> list[WebhookSubscription]:
        """Return subscriptions, optionally filtered by deployment and/or tenant."""
        where = {"deployment_ref": deployment_ref} if deployment_ref is not None else None
        async with self._subscriptions.transaction() as subscriptions:
            rows = await subscriptions.select(where=where, order_by=("created_at",))
        return [self._row_to_subscription(r) for r in rows]

    async def list_subscriptions_for_event(
        self,
        deployment_ref: str,
        event_type: WebhookEventType,
    ) -> list[WebhookSubscription]:
        """Return active subscriptions for a deployment matching the given event type."""
        async with self._subscriptions.transaction() as subscriptions:
            rows = await subscriptions.select(where={"deployment_ref": deployment_ref, "active": 1})
        result: list[WebhookSubscription] = []
        for row in rows:
            sub = self._row_to_subscription(row)
            if event_type in sub.event_types:
                result.append(sub)
        return result

    async def deactivate_subscription(self, subscription_id: str) -> None:
        """Set a subscription to inactive."""
        now = utc_now().isoformat()
        await self._subscriptions.update(
            {"active": 0, "updated_at": now}, where={"subscription_id": subscription_id}
        )

    async def delete_subscription(self, subscription_id: str) -> None:
        """Hard-delete a subscription."""
        await self._subscriptions.delete(where={"subscription_id": subscription_id})

    # ── Delivery lifecycle ─────────────────────────────────────────────

    async def enqueue_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        """Persist a new delivery with PENDING status."""
        await self._deliveries.insert(
            {
                "delivery_id": delivery.delivery_id,
                "subscription_id": delivery.subscription_id,
                "event_type": delivery.event_type.value,
                "event_id": delivery.event_id,
                "payload_json": delivery.payload_json,
                "status": delivery.status.value,
                "attempt_count": delivery.attempt_count,
                "max_attempts": delivery.max_attempts,
                "next_attempt_at": delivery.next_attempt_at.isoformat(),
                "last_error": delivery.last_error,
                "last_status_code": delivery.last_status_code,
                "created_at": delivery.created_at.isoformat(),
                "updated_at": delivery.updated_at.isoformat(),
            }
        )
        return delivery

    async def claim_pending_delivery(
        self, *, lease_seconds: float = 30.0
    ) -> WebhookDelivery | None:
        """Claim the oldest delivery that is due for an attempt.

        Atomically leases the delivery so the polling worker cannot
        double-claim (and thus double-deliver) it: within one transaction
        the chosen row is flipped to ``DELIVERING`` and its
        ``next_attempt_at`` is pushed ``lease_seconds`` into the future, so a
        subsequent claim won't see it until the lease lapses. The
        SELECT-then-UPDATE is atomic for a single serialized poll loop;
        concurrent workers would additionally need row-level locking (e.g.
        ``SELECT ... FOR UPDATE SKIP LOCKED``) to keep the same guarantee.
        A claim covers three due cases:

        * ``PENDING`` -- reached its first-attempt time;
        * ``FAILED``  -- its retry backoff has elapsed (the actual retry path);
        * ``DELIVERING`` -- its lease expired, i.e. a worker died mid-delivery.

        Returns the claimed delivery, or ``None`` when nothing is due.
        """
        now = utc_now()
        now_iso = now.isoformat()
        eligible = {
            DeliveryStatus.PENDING.value,
            DeliveryStatus.FAILED.value,
            DeliveryStatus.DELIVERING.value,
        }
        async with self._deliveries.transaction(write_lock=True) as deliveries:
            rows = await deliveries.select(
                where_lt={"next_attempt_at": now_iso}, order_by=("next_attempt_at",)
            )
            row = next((candidate for candidate in rows if candidate["status"] in eligible), None)
            if row is None:
                return None
            delivery = self._row_to_delivery(row)
            lease_until = now + timedelta(seconds=lease_seconds)
            await deliveries.update(
                {
                    "status": DeliveryStatus.DELIVERING.value,
                    "next_attempt_at": lease_until.isoformat(),
                    "updated_at": now_iso,
                },
                where={"delivery_id": delivery.delivery_id},
            )
        return delivery

    async def mark_delivered(self, delivery_id: str) -> None:
        """Mark a delivery as successfully delivered."""
        now = utc_now().isoformat()
        await self._deliveries.update(
            {"status": DeliveryStatus.DELIVERED.value, "updated_at": now},
            where={"delivery_id": delivery_id},
        )

    async def mark_failed(
        self,
        delivery_id: str,
        *,
        error: str,
        status_code: int | None,
        retry_delay: float,
    ) -> None:
        """Mark a delivery as failed and schedule the next retry.

        Increments attempt_count and schedules ``next_attempt_at`` at
        ``retry_delay`` seconds from now, then records the error details.
        ``retry_delay`` is the final, already-jittered backoff in seconds:
        the delivery worker owns the backoff policy (see ``next_retry_delay``)
        and this method persists it verbatim rather than re-deriving it.
        """
        now = utc_now()
        async with self._deliveries.transaction(write_lock=True) as deliveries:
            row = await deliveries.select_one(
                where={"delivery_id": delivery_id}, columns=("attempt_count",)
            )
            if row is None:
                return
            new_count = row["attempt_count"] + 1
            next_at = now + timedelta(seconds=retry_delay)
            await deliveries.update(
                {
                    "status": DeliveryStatus.FAILED.value,
                    "attempt_count": new_count,
                    "next_attempt_at": next_at.isoformat(),
                    "last_error": error,
                    "last_status_code": status_code,
                    "updated_at": now.isoformat(),
                },
                where={"delivery_id": delivery_id},
            )

    async def dead_letter(self, delivery_id: str) -> None:
        """Move a delivery to the dead-letter table.

        Inserts into webhook_dead_letters from delivery data, then updates
        the delivery status to DEAD_LETTER.
        """
        now = utc_now()
        async with self._deliveries.transaction(write_lock=True) as deliveries:
            dead_letters = deliveries.bind(self._dead_letters)
            row = await deliveries.select_one(where={"delivery_id": delivery_id})
            if row is None:
                return
            dead_letter_id = _new_id()
            await dead_letters.insert(
                {
                    "dead_letter_id": dead_letter_id,
                    "delivery_id": row["delivery_id"],
                    "subscription_id": row["subscription_id"],
                    "event_type": row["event_type"],
                    "event_id": row["event_id"],
                    "payload_json": row["payload_json"],
                    "attempt_count": row["attempt_count"],
                    "last_error": row["last_error"],
                    "last_status_code": row["last_status_code"],
                    "created_at": row["created_at"],
                    "dead_lettered_at": now.isoformat(),
                }
            )
            await deliveries.update(
                {
                    "status": DeliveryStatus.DEAD_LETTER.value,
                    "updated_at": now.isoformat(),
                },
                where={"delivery_id": delivery_id},
            )

    # ── Dead-letter queries ────────────────────────────────────────────

    async def list_dead_letters(
        self,
        subscription_id: str | None = None,
        limit: int = 50,
        subscription_ids: Sequence[str] | None = None,
    ) -> list[WebhookDeadLetter]:
        """Return dead-letter entries, optionally filtered by subscription.

        ``subscription_ids`` restricts the query to a set of subscriptions so the
        LIMIT is applied AFTER the tenant scope (audit F8 re-audit) — filtering in
        Python after a global LIMIT would silently hide a deployment's own rows
        behind newer foreign ones.
        """
        where = {"subscription_id": subscription_id} if subscription_id is not None else None
        if subscription_ids is not None and not subscription_ids:
            return []
        async with self._dead_letters.transaction() as dead_letters:
            rows = await dead_letters.select(where=where, order_by=("dead_lettered_at",))
        if subscription_ids is not None:
            allowed = set(subscription_ids)
            rows = [row for row in rows if row["subscription_id"] in allowed]
        rows = list(reversed(rows))[:limit]
        return [self._row_to_dead_letter(r) for r in rows]

    async def get_dead_letter(self, dead_letter_id: str) -> WebhookDeadLetter | None:
        """Look up a single dead-letter entry by ID."""
        row = await self._dead_letters.select_one(where={"dead_letter_id": dead_letter_id})
        if row is None:
            return None
        return self._row_to_dead_letter(row)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_subscription(row: dict) -> WebhookSubscription:
        """Convert a database row to a WebhookSubscription model."""
        event_types_raw = load_typed_value(row["event_types"], list)
        return WebhookSubscription(
            subscription_id=row["subscription_id"],
            deployment_ref=row["deployment_ref"],
            tenant_id=row["tenant_id"],
            target_url=row["target_url"],
            secret=row["secret"],
            event_types=[WebhookEventType(e) for e in event_types_raw],
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_delivery(row: dict) -> WebhookDelivery:
        """Convert a database row to a WebhookDelivery model."""
        return WebhookDelivery(
            delivery_id=row["delivery_id"],
            subscription_id=row["subscription_id"],
            event_type=WebhookEventType(row["event_type"]),
            event_id=row["event_id"],
            payload_json=row["payload_json"],
            status=DeliveryStatus(row["status"]),
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
            last_error=row["last_error"],
            last_status_code=row["last_status_code"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_dead_letter(row: dict) -> WebhookDeadLetter:
        """Convert a database row to a WebhookDeadLetter model."""
        return WebhookDeadLetter(
            dead_letter_id=row["dead_letter_id"],
            delivery_id=row["delivery_id"],
            subscription_id=row["subscription_id"],
            event_type=WebhookEventType(row["event_type"]),
            event_id=row["event_id"],
            payload_json=row["payload_json"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            last_status_code=row["last_status_code"],
            created_at=datetime.fromisoformat(row["created_at"]),
            dead_lettered_at=datetime.fromisoformat(row["dead_lettered_at"]),
        )
