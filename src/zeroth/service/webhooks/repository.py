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
from zeroth.platform.storage.database import AsyncDatabase
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

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    # ── Subscription CRUD ──────────────────────────────────────────────

    async def create_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        """Persist a new webhook subscription and return it."""
        async with self._database.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO webhook_subscriptions (
                    subscription_id, deployment_ref, tenant_id, target_url,
                    secret, event_types, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub.subscription_id,
                    sub.deployment_ref,
                    sub.tenant_id,
                    sub.target_url,
                    sub.secret,
                    to_json_value(list(sub.event_types)),
                    1 if sub.active else 0,
                    sub.created_at.isoformat(),
                    sub.updated_at.isoformat(),
                ),
            )
        return sub

    async def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        """Look up a subscription by ID. Returns None if not found."""
        async with self._database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT * FROM webhook_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            )
        if row is None:
            return None
        return self._row_to_subscription(row)

    async def list_subscriptions(
        self,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> list[WebhookSubscription]:
        """Return subscriptions, optionally filtered by deployment and/or tenant."""
        clauses: list[str] = []
        params: list[str] = []
        if deployment_ref is not None:
            clauses.append("deployment_ref = ?")
            params.append(deployment_ref)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        sql = "SELECT * FROM webhook_subscriptions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        async with self._database.transaction() as conn:
            rows = await conn.fetch_all(sql, tuple(params))
        return [self._row_to_subscription(r) for r in rows]

    async def list_subscriptions_for_event(
        self,
        deployment_ref: str,
        event_type: WebhookEventType,
    ) -> list[WebhookSubscription]:
        """Return active subscriptions for a deployment matching the given event type."""
        async with self._database.transaction() as conn:
            rows = await conn.fetch_all(
                "SELECT * FROM webhook_subscriptions WHERE deployment_ref = ? AND active = 1",
                (deployment_ref,),
            )
        result: list[WebhookSubscription] = []
        for row in rows:
            sub = self._row_to_subscription(row)
            if event_type in sub.event_types:
                result.append(sub)
        return result

    async def deactivate_subscription(self, subscription_id: str) -> None:
        """Set a subscription to inactive."""
        now = utc_now().isoformat()
        async with self._database.transaction() as conn:
            await conn.execute(
                "UPDATE webhook_subscriptions SET active = 0, updated_at = ? "
                "WHERE subscription_id = ?",
                (now, subscription_id),
            )

    async def delete_subscription(self, subscription_id: str) -> None:
        """Hard-delete a subscription."""
        async with self._database.transaction() as conn:
            await conn.execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            )

    # ── Delivery lifecycle ─────────────────────────────────────────────

    async def enqueue_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        """Persist a new delivery with PENDING status."""
        async with self._database.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO webhook_deliveries (
                    delivery_id, subscription_id, event_type, event_id,
                    payload_json, status, attempt_count, max_attempts,
                    next_attempt_at, last_error, last_status_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery.delivery_id,
                    delivery.subscription_id,
                    delivery.event_type.value,
                    delivery.event_id,
                    delivery.payload_json,
                    delivery.status.value,
                    delivery.attempt_count,
                    delivery.max_attempts,
                    delivery.next_attempt_at.isoformat(),
                    delivery.last_error,
                    delivery.last_status_code,
                    delivery.created_at.isoformat(),
                    delivery.updated_at.isoformat(),
                ),
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
        async with self._database.transaction() as conn:
            row = await conn.fetch_one(
                """
                SELECT * FROM webhook_deliveries
                WHERE next_attempt_at <= ?
                  AND status IN (?, ?, ?)
                ORDER BY next_attempt_at
                LIMIT 1
                """,
                (
                    now_iso,
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.FAILED.value,
                    DeliveryStatus.DELIVERING.value,
                ),
            )
            if row is None:
                return None
            delivery = self._row_to_delivery(row)
            lease_until = now + timedelta(seconds=lease_seconds)
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = ?, next_attempt_at = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    DeliveryStatus.DELIVERING.value,
                    lease_until.isoformat(),
                    now_iso,
                    delivery.delivery_id,
                ),
            )
        return delivery

    async def mark_delivered(self, delivery_id: str) -> None:
        """Mark a delivery as successfully delivered."""
        now = utc_now().isoformat()
        async with self._database.transaction() as conn:
            await conn.execute(
                "UPDATE webhook_deliveries SET status = ?, updated_at = ? WHERE delivery_id = ?",
                (DeliveryStatus.DELIVERED.value, now, delivery_id),
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
        async with self._database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT attempt_count FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            if row is None:
                return
            new_count = row["attempt_count"] + 1
            next_at = now + timedelta(seconds=retry_delay)
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = ?, attempt_count = ?, next_attempt_at = ?,
                    last_error = ?, last_status_code = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    DeliveryStatus.FAILED.value,
                    new_count,
                    next_at.isoformat(),
                    error,
                    status_code,
                    now.isoformat(),
                    delivery_id,
                ),
            )

    async def dead_letter(self, delivery_id: str) -> None:
        """Move a delivery to the dead-letter table.

        Inserts into webhook_dead_letters from delivery data, then updates
        the delivery status to DEAD_LETTER.
        """
        now = utc_now()
        async with self._database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            if row is None:
                return
            dead_letter_id = _new_id()
            await conn.execute(
                """
                INSERT INTO webhook_dead_letters (
                    dead_letter_id, delivery_id, subscription_id, event_type,
                    event_id, payload_json, attempt_count, last_error,
                    last_status_code, created_at, dead_lettered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dead_letter_id,
                    row["delivery_id"],
                    row["subscription_id"],
                    row["event_type"],
                    row["event_id"],
                    row["payload_json"],
                    row["attempt_count"],
                    row["last_error"],
                    row["last_status_code"],
                    row["created_at"],
                    now.isoformat(),
                ),
            )
            await conn.execute(
                "UPDATE webhook_deliveries SET status = ?, updated_at = ? WHERE delivery_id = ?",
                (DeliveryStatus.DEAD_LETTER.value, now.isoformat(), delivery_id),
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
        if subscription_id is not None:
            sql = (
                "SELECT * FROM webhook_dead_letters "
                "WHERE subscription_id = ? ORDER BY dead_lettered_at DESC LIMIT ?"
            )
            params: tuple = (subscription_id, limit)
        elif subscription_ids is not None:
            if not subscription_ids:
                return []
            placeholders = ",".join("?" for _ in subscription_ids)
            sql = (
                f"SELECT * FROM webhook_dead_letters WHERE subscription_id IN ({placeholders}) "
                "ORDER BY dead_lettered_at DESC LIMIT ?"
            )
            params = (*subscription_ids, limit)
        else:
            sql = "SELECT * FROM webhook_dead_letters ORDER BY dead_lettered_at DESC LIMIT ?"
            params = (limit,)
        async with self._database.transaction() as conn:
            rows = await conn.fetch_all(sql, params)
        return [self._row_to_dead_letter(r) for r in rows]

    async def get_dead_letter(self, dead_letter_id: str) -> WebhookDeadLetter | None:
        """Look up a single dead-letter entry by ID."""
        async with self._database.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT * FROM webhook_dead_letters WHERE dead_letter_id = ?",
                (dead_letter_id,),
            )
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
