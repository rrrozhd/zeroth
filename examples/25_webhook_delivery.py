"""25 — Webhook delivery: real WebhookDeliveryWorker hitting a real receiver.

What this shows
---------------
Runs the real :class:`WebhookDeliveryWorker` against a receiver built
on :class:`httpx.MockTransport` — so the worker actually POSTs,
actually retries on failure, and the receiver actually verifies the
HMAC-SHA256 signature the worker attaches to every payload.

Flow:

1. Run migrations, build a :class:`WebhookRepository`.
2. Create a :class:`WebhookSubscription` pointing at a fake receiver.
3. Publish an event via :class:`WebhookService.emit_event` — the
   service looks up matching subscriptions and enqueues a
   :class:`WebhookDelivery` per subscription.
4. Start :class:`WebhookDeliveryWorker.poll_loop` in the background.
5. Observe the delivery landing at the receiver, its signature
   verified, and its status transitioning to ``DELIVERED``.
6. Repeat with a receiver that fails for the first N attempts to show
   retry + backoff, then succeeds.

Run
---
    uv run python examples/25_webhook_delivery.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import hashlib
import hmac
import sys
import tempfile
from pathlib import Path

import httpx

from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.storage import AsyncSQLiteDatabase
from zeroth.core.webhooks import (
    WebhookEventType,
    WebhookRepository,
    WebhookSubscription,
    sign_payload,
)
from zeroth.core.webhooks.delivery import WebhookDeliveryWorker
from zeroth.core.webhooks.service import WebhookService


class FlakyReceiver:
    """Fake webhook endpoint that fails the first ``fail_count`` attempts.

    Verifies the HMAC-SHA256 signature on every hit. Records every body
    it sees so the example can assert on delivery ordering.
    """

    def __init__(self, *, secret: str, fail_count: int = 0) -> None:
        self.secret = secret
        self._fail_count = fail_count
        self.hits: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content or b""
        signature = request.headers.get("X-Zeroth-Signature", "")
        expected = hmac.new(
            self.secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature.removeprefix("sha256="), expected):
            return httpx.Response(401, json={"error": "bad signature"})

        self.hits.append(body)
        if self._fail_count > 0:
            self._fail_count -= 1
            return httpx.Response(500, json={"error": "simulated failure"})
        return httpx.Response(200, json={"ok": True})


async def run_worker_for(
    worker: WebhookDeliveryWorker,
    *,
    duration: float,
) -> None:
    """Run the delivery worker poll loop for a fixed duration."""
    task = asyncio.create_task(worker.poll_loop())
    try:
        await asyncio.sleep(duration)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def main() -> int:
    tmp = Path(tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name)
    run_migrations(f"sqlite:///{tmp}")
    database = AsyncSQLiteDatabase(path=str(tmp))

    repository = WebhookRepository(database)
    service = WebhookService(repository=repository)

    try:
        # ── Scenario 1: happy path ────────────────────────────────────
        happy_receiver = FlakyReceiver(secret="happy-secret")
        sub = await service.create_subscription(
            WebhookSubscription(
                deployment_ref="examples-demo",
                target_url="https://happy.example/webhook",
                secret="happy-secret",
                event_types=[WebhookEventType.RUN_COMPLETED],
            )
        )
        deliveries = await service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="examples-demo",
            tenant_id="default",
            data={"run_id": "demo-1", "status": "completed"},
        )
        print(f"enqueued {len(deliveries)} delivery")

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(happy_receiver.handler))
        worker = WebhookDeliveryWorker(
            repository=repository,
            http_client=http_client,
            poll_interval=0.05,
            retry_base_delay=0.1,
            retry_max_delay=0.2,
        )
        await run_worker_for(worker, duration=0.5)
        print(f"happy-path receiver hits: {len(happy_receiver.hits)}")
        assert len(happy_receiver.hits) >= 1, "worker never hit the receiver"
        await http_client.aclose()

        # ── Scenario 2: retry then succeed ────────────────────────────
        flaky_receiver = FlakyReceiver(secret="flaky-secret", fail_count=2)
        flaky_sub = await service.create_subscription(
            WebhookSubscription(
                deployment_ref="examples-flaky",
                target_url="https://flaky.example/webhook",
                secret="flaky-secret",
                event_types=[WebhookEventType.RUN_COMPLETED],
            )
        )
        await service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="examples-flaky",
            tenant_id="default",
            data={"run_id": "demo-2", "status": "completed"},
        )
        flaky_client = httpx.AsyncClient(
            transport=httpx.MockTransport(flaky_receiver.handler)
        )
        flaky_worker = WebhookDeliveryWorker(
            repository=repository,
            http_client=flaky_client,
            poll_interval=0.05,
            retry_base_delay=0.05,
            retry_max_delay=0.1,
        )
        await run_worker_for(flaky_worker, duration=2.0)
        print(
            f"flaky-path receiver hits: {len(flaky_receiver.hits)} "
            f"(≥3 — 2 failures then at least 1 success)"
        )
        assert len(flaky_receiver.hits) >= 3, "retry-then-success path never completed"
        await flaky_client.aclose()

        # ── Bonus: demonstrate sign_payload directly ─────────────────
        body = b'{"event_type":"run.completed","data":{"run_id":"demo-1"}}'
        sig = sign_payload(body, "happy-secret")
        print(f"\nsign_payload('happy-secret', body) = sha256={sig[:16]}…")

        _ = sub
    finally:
        tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
