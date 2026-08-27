"""Campaign-local webhook receiver used by the live product-surface evaluation.

The transport never opens a network socket. It accepts only the reserved campaign
host/path, verifies the production HMAC header against the persisted subscription,
and stores sanitized receipts in an external SQLite database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zeroth.service.webhooks.signing import sign_payload

EVALUATION_WEBHOOK_HOST = "example.com"
EVALUATION_WEBHOOK_PREFIX = "/zeroth-evaluation/"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EvaluationWebhookSink:
    """Append-only sanitized delivery attempts plus idempotent success receipts."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS webhook_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL,
                    subscription_id TEXT,
                    event_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    signature_verified INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webhook_receipts (
                    delivery_id TEXT PRIMARY KEY,
                    subscription_id TEXT,
                    event_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE,
                    signature_verified INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Schema inspection and additive migration are one serialized fact.
            # Without the write lock, two dev/evidence processes can both see
            # an old column set and the loser crashes on a duplicate ALTER.
            connection.execute("BEGIN IMMEDIATE")
            # Preserve older campaign sinks while adding logical-event identity.
            # SQLite cannot add a UNIQUE NOT NULL column in place, so the partial
            # index applies to newly correlated receipts and old rows are adopted
            # on their next matching delivery.
            for table, column in (
                ("webhook_attempts", "event_id"),
                ("webhook_receipts", "event_id"),
                ("webhook_attempts", "subscription_id"),
                ("webhook_receipts", "subscription_id"),
            ):
                columns = {
                    str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            connection.execute("DROP INDEX IF EXISTS ux_webhook_receipts_event_id")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_receipts_subscription_event
                ON webhook_receipts (subscription_id, event_id)
                WHERE subscription_id IS NOT NULL AND event_id IS NOT NULL
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def record_attempt(
        self,
        *,
        delivery_id: str,
        subscription_id: str,
        event_id: str,
        event_type: str,
        payload_hash: str,
        signature_verified: bool,
        outcome: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO webhook_attempts (
                    delivery_id, subscription_id, event_id, event_type, payload_hash,
                    signature_verified, outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    subscription_id,
                    event_id,
                    event_type,
                    payload_hash,
                    int(signature_verified),
                    outcome,
                    _utc_now(),
                ),
            )

    def commit_receipt(
        self,
        *,
        delivery_id: str,
        subscription_id: str,
        event_id: str,
        event_type: str,
        payload_hash: str,
    ) -> tuple[str, bool]:
        """Commit one durable receipt, returning the prior one for exact duplicates."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_id, payload_hash, event_id, subscription_id
                FROM webhook_receipts
                WHERE subscription_id = ? AND event_id = ?
                """,
                (subscription_id, event_id),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT receipt_id, payload_hash, event_id, subscription_id
                    FROM webhook_receipts
                    WHERE delivery_id = ?
                    """,
                    (delivery_id,),
                ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise ValueError("event payload hash changed")
                if row["event_id"] is None or row["subscription_id"] is None:
                    connection.execute(
                        """
                        UPDATE webhook_receipts
                        SET subscription_id = ?, event_id = ?
                        WHERE delivery_id = ?
                        """,
                        (subscription_id, event_id, delivery_id),
                    )
                return str(row["receipt_id"]), True
            receipt_id = f"webhook-receipt-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO webhook_receipts (
                    delivery_id, subscription_id, event_id, event_type, payload_hash, receipt_id,
                    signature_verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    delivery_id,
                    subscription_id,
                    event_id,
                    event_type,
                    payload_hash,
                    receipt_id,
                    _utc_now(),
                ),
            )
            return receipt_id, False

    def outcome_count(self, outcome: str) -> int:
        """Return the durable attempt count for one controlled failure scenario."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM webhook_attempts WHERE outcome = ?",
                (outcome,),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    async def wait_for_outcome(
        self,
        outcome: str,
        *,
        poll_interval: float = 0.01,
    ) -> None:
        """Wait until a controlled hook's durable marker is observable."""
        while self.outcome_count(outcome) < 1:
            await asyncio.sleep(poll_interval)

    def receipts(self) -> list[dict[str, Any]]:
        """Return sanitized receipt summaries; payloads and headers never leave the sink."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.delivery_id, r.subscription_id, r.event_id, r.event_type,
                       r.payload_hash, r.receipt_id,
                       r.signature_verified, r.created_at,
                       (
                           SELECT COUNT(*) FROM webhook_attempts AS a
                           WHERE (
                               r.subscription_id IS NOT NULL
                               AND r.event_id IS NOT NULL
                               AND a.subscription_id = r.subscription_id
                               AND a.event_id = r.event_id
                           ) OR (
                               (r.subscription_id IS NULL OR r.event_id IS NULL)
                               AND a.delivery_id = r.delivery_id
                           )
                       ) AS attempt_count
                FROM webhook_receipts AS r
                ORDER BY r.created_at DESC
                """
            ).fetchall()
        return [
            {
                "delivery_id": row["delivery_id"],
                "subscription_id": row["subscription_id"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "payload_hash": row["payload_hash"],
                "receipt_id": row["receipt_id"],
                "signature_verified": bool(row["signature_verified"]),
                "created_at": row["created_at"],
                "attempt_count": int(row["attempt_count"]),
            }
            for row in rows
        ]


class EvaluationWebhookTransport(httpx.AsyncBaseTransport):
    """Socket-free httpx transport backed by :class:`EvaluationWebhookSink`."""

    def __init__(self, *, repository: Any, sink: EvaluationWebhookSink) -> None:
        self.repository = repository
        self.sink = sink

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("Host", "").split(":", 1)[0].lower()
        path = request.url.path
        if host != EVALUATION_WEBHOOK_HOST or not path.startswith(EVALUATION_WEBHOOK_PREFIX):
            return httpx.Response(421, request=request)

        delivery_id = request.headers.get("X-Zeroth-Delivery", "")
        delivery = await self.repository.get_delivery(delivery_id)
        if delivery is None:
            return httpx.Response(404, request=request)
        subscription = await self.repository.get_subscription(delivery.subscription_id)
        if subscription is None:
            return httpx.Response(404, request=request)

        payload = request.content
        payload_hash = hashlib.sha256(payload).hexdigest()
        expected = f"sha256={sign_payload(payload, subscription.secret)}"
        supplied = request.headers.get("X-Zeroth-Signature", "")
        verified = hmac.compare_digest(expected, supplied)
        event_type = request.headers.get("X-Zeroth-Event", delivery.event_type.value)
        event_id = delivery.event_id
        hook = path.removeprefix(EVALUATION_WEBHOOK_PREFIX).strip("/")
        mode, _, scenario = hook.partition("/")

        if not verified:
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=False,
                outcome="invalid_signature",
            )
            return httpx.Response(401, request=request)

        if mode == "timeout":
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="timeout",
            )
            raise httpx.ReadTimeout("controlled evaluation timeout", request=request)
        if mode == "unavailable":
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="unavailable",
            )
            raise httpx.ConnectError("controlled evaluation sink unavailable", request=request)
        if mode == "non2xx":
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="http_503",
            )
            return httpx.Response(503, request=request)
        if mode == "flaky":
            outcome = f"flaky:{scenario or 'default'}"
            if self.sink.outcome_count(outcome) < 5:
                self.sink.record_attempt(
                    delivery_id=delivery_id,
                    subscription_id=delivery.subscription_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload_hash=payload_hash,
                    signature_verified=True,
                    outcome=outcome,
                )
                return httpx.Response(503, request=request)
            mode = "success"
        if mode == "restart-after-lease":
            outcome = f"leased_before_restart:{scenario or 'default'}"
            if self.sink.outcome_count(outcome) < 1:
                self.sink.record_attempt(
                    delivery_id=delivery_id,
                    subscription_id=delivery.subscription_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload_hash=payload_hash,
                    signature_verified=True,
                    outcome=outcome,
                )
                # This task is intentionally released only by process
                # termination.  The delivery lease remains durable; after it
                # expires, the restarted worker retries and observes the marker.
                await asyncio.Event().wait()
            mode = "success"
        if mode not in {"success", "timeout-after-commit"}:
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="unknown_hook",
            )
            return httpx.Response(404, request=request)

        if mode == "timeout-after-commit":
            try:
                receipt_id, duplicate = self.sink.commit_receipt(
                    delivery_id=delivery_id,
                    subscription_id=delivery.subscription_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload_hash=payload_hash,
                )
            except ValueError:
                self.sink.record_attempt(
                    delivery_id=delivery_id,
                    subscription_id=delivery.subscription_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload_hash=payload_hash,
                    signature_verified=True,
                    outcome="payload_hash_conflict",
                )
                return httpx.Response(409, request=request)
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="duplicate" if duplicate else "timeout_after_commit",
            )
            if not duplicate:
                raise httpx.ReadTimeout(
                    "controlled evaluation timeout after commit",
                    request=request,
                )
            return httpx.Response(
                204,
                request=request,
                headers={
                    "X-Zeroth-Evaluation-Receipt": receipt_id,
                    "X-Zeroth-Evaluation-Duplicate": "true",
                },
            )
        try:
            receipt_id, duplicate = self.sink.commit_receipt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
            )
        except ValueError:
            self.sink.record_attempt(
                delivery_id=delivery_id,
                subscription_id=delivery.subscription_id,
                event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                signature_verified=True,
                outcome="payload_hash_conflict",
            )
            return httpx.Response(409, request=request)
        self.sink.record_attempt(
            delivery_id=delivery_id,
            subscription_id=delivery.subscription_id,
            event_id=event_id,
            event_type=event_type,
            payload_hash=payload_hash,
            signature_verified=True,
            outcome="duplicate" if duplicate else "delivered",
        )
        return httpx.Response(
            204,
            request=request,
            headers={
                "X-Zeroth-Evaluation-Receipt": receipt_id,
                "X-Zeroth-Evaluation-Duplicate": str(duplicate).lower(),
            },
        )
