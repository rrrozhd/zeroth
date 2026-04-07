"""Background webhook delivery worker with retry and dead-lettering.

Polls for pending deliveries, sends HTTP POST with HMAC signature,
and handles retries with exponential backoff and jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from zeroth.webhooks.models import WebhookDelivery
from zeroth.webhooks.repository import WebhookRepository
from zeroth.webhooks.signing import sign_payload

logger = logging.getLogger(__name__)


def next_retry_delay(attempt: int, base: float = 1.0, max_delay: float = 300.0) -> float:
    """Compute jittered exponential backoff delay.

    Returns a value in the range [0, min(base * 2^attempt, max_delay)].
    """
    delay = min(base * (2**attempt), max_delay)
    return random.uniform(0, delay)  # noqa: S311


@dataclass
class WebhookDeliveryWorker:
    """Background worker that polls for pending deliveries and sends HTTP POST requests."""

    repository: WebhookRepository
    http_client: httpx.AsyncClient
    poll_interval: float = 2.0
    max_concurrency: int = 16
    retry_base_delay: float = 1.0
    retry_max_delay: float = 300.0

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_tasks: set[asyncio.Task] = set()

    async def poll_loop(self) -> None:
        """Continuously claim and deliver pending webhooks until cancelled."""
        while True:
            try:
                delivery = await self.repository.claim_pending_delivery()
                if delivery is not None:
                    await self._semaphore.acquire()
                    task = asyncio.create_task(
                        self._deliver_with_semaphore(delivery),
                        name=f"webhook-{delivery.delivery_id}",
                    )
                    self._active_tasks.add(task)
                    task.add_done_callback(self._active_tasks.discard)
                else:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook delivery poll error")
                await asyncio.sleep(self.poll_interval)

    async def _deliver_with_semaphore(self, delivery: WebhookDelivery) -> None:
        """Deliver a webhook and release the semaphore afterwards."""
        try:
            await self._deliver(delivery)
        finally:
            self._semaphore.release()

    async def _deliver(self, delivery: WebhookDelivery) -> None:
        """Send an HTTP POST for a single delivery."""
        sub = await self.repository.get_subscription(delivery.subscription_id)
        if sub is None:
            logger.warning(
                "subscription %s not found for delivery %s",
                delivery.subscription_id,
                delivery.delivery_id,
            )
            await self.repository.mark_failed(
                delivery.delivery_id,
                error="subscription not found",
                status_code=None,
                retry_delay=0,
            )
            return
        payload_bytes = delivery.payload_json.encode("utf-8")
        signature = sign_payload(payload_bytes, sub.secret)
        headers = {
            "Content-Type": "application/json",
            "X-Zeroth-Signature": f"sha256={signature}",
            "X-Zeroth-Event": delivery.event_type.value,
            "X-Zeroth-Delivery": delivery.delivery_id,
        }
        try:
            response = await self.http_client.post(
                sub.target_url, content=payload_bytes, headers=headers
            )
            if 200 <= response.status_code < 300:
                await self.repository.mark_delivered(delivery.delivery_id)
            else:
                await self._handle_failure(
                    delivery,
                    error=f"HTTP {response.status_code}",
                    status_code=response.status_code,
                )
        except httpx.TimeoutException:
            await self._handle_failure(delivery, error="timeout", status_code=None)
        except httpx.HTTPError as exc:
            await self._handle_failure(delivery, error=str(exc), status_code=None)

    async def _handle_failure(
        self,
        delivery: WebhookDelivery,
        *,
        error: str,
        status_code: int | None,
    ) -> None:
        """Handle a failed delivery: retry or dead-letter."""
        next_attempt = delivery.attempt_count + 1
        if next_attempt >= delivery.max_attempts:
            await self.repository.dead_letter(delivery.delivery_id)
            logger.warning(
                "webhook delivery %s dead-lettered after %d attempts",
                delivery.delivery_id,
                next_attempt,
            )
        else:
            delay = next_retry_delay(
                delivery.attempt_count, self.retry_base_delay, self.retry_max_delay
            )
            await self.repository.mark_failed(
                delivery.delivery_id,
                error=error,
                status_code=status_code,
                retry_delay=delay,
            )
