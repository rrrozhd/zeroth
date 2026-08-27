"""Background webhook delivery worker with retry and dead-lettering.

Polls for pending deliveries, sends HTTP POST with HMAC signature,
and handles retries with exponential backoff and jitter.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass
from uuid import uuid4

import httpx

from zeroth.platform.primitives.boundary import (
    OutboundDestinationError,
    resolve_outbound_url,
)
from zeroth.service.service_audit import ServiceAuditRecorder, webhook_event_identity
from zeroth.service.webhooks.models import WebhookDelivery
from zeroth.service.webhooks.repository import WebhookRepository
from zeroth.service.webhooks.signing import sign_payload

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
    audit_recorder: ServiceAuditRecorder | None = None
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
                claim = await self.repository.claim_pending_delivery()
                if claim is not None:
                    await self._semaphore.acquire()
                    task = asyncio.create_task(
                        self._deliver_with_semaphore(claim.delivery, claim.generation),
                        name=f"webhook-{claim.delivery.delivery_id}",
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

    async def _deliver_with_semaphore(self, delivery: WebhookDelivery, generation: int) -> None:
        """Deliver a webhook and release the semaphore afterwards."""
        try:
            await self._deliver(delivery, generation)
        finally:
            self._semaphore.release()

    async def _deliver(self, delivery: WebhookDelivery, generation: int) -> None:
        """Send an HTTP POST for a single delivery."""
        sub = await self.repository.get_subscription(delivery.subscription_id)
        if sub is None:
            logger.warning(
                "subscription %s not found for delivery %s",
                delivery.subscription_id,
                delivery.delivery_id,
            )
            audit_record, audit_repository = self._transition_audit(delivery, "delivery_failed")
            await self.repository.mark_failed(
                delivery.delivery_id,
                generation,
                error="subscription not found",
                status_code=None,
                retry_delay=0,
                audit_record=audit_record,
                audit_repository=audit_repository,
            )
            return
        # A02-6, defence in depth: creation-time validation cannot reach a row
        # persisted before that bound existed, and such a row is exactly the
        # SSRF primitive the finding describes. Dead-lettered rather than
        # retried -- a target that names internal infrastructure will not become
        # legitimate on the next attempt.
        try:
            approved = resolve_outbound_url(sub.target_url, context="webhook target_url")
        except OutboundDestinationError:
            logger.warning(
                "refusing delivery %s: subscription %s has an unsafe or unavailable destination",
                delivery.delivery_id,
                sub.subscription_id,
            )
            dead_letter_id = uuid4().hex
            audit_record, audit_repository = self._transition_audit(
                delivery,
                "delivery_dead_lettered",
                dead_letter_id=dead_letter_id,
            )
            await self.repository.dead_letter(
                delivery.delivery_id,
                generation,
                dead_letter_id=dead_letter_id,
                audit_record=audit_record,
                audit_repository=audit_repository,
            )
            return
        payload_bytes = delivery.payload_json.encode("utf-8")
        signature = sign_payload(payload_bytes, sub.secret)
        headers = {
            "Content-Type": "application/json",
            "X-Zeroth-Signature": f"sha256={signature}",
            "X-Zeroth-Event": delivery.event_type.value,
            "X-Zeroth-Delivery": delivery.delivery_id,
            "Host": approved.host_header,
            # The pooled client's origin key is the pinned IP. Closing each
            # connection prevents a later hostname on the same shared IP from
            # reusing TLS established with another hostname's SNI.
            "Connection": "close",
        }
        try:
            response = await self.http_client.post(
                approved.connect_url,
                content=payload_bytes,
                headers=headers,
                follow_redirects=False,
                extensions={"sni_hostname": approved.sni_hostname},
            )
            if 200 <= response.status_code < 300:
                audit_record, audit_repository = self._transition_audit(
                    delivery,
                    "delivery_delivered",
                    upstream_status_code=response.status_code,
                )
                await self.repository.mark_delivered(
                    delivery.delivery_id,
                    generation,
                    audit_record=audit_record,
                    audit_repository=audit_repository,
                )
            else:
                await self._handle_failure(
                    delivery,
                    generation,
                    error=f"HTTP {response.status_code}",
                    status_code=response.status_code,
                )
        except httpx.TimeoutException:
            await self._handle_failure(delivery, generation, error="timeout", status_code=None)
        except httpx.HTTPError as exc:
            await self._handle_failure(delivery, generation, error=str(exc), status_code=None)

    async def _handle_failure(
        self,
        delivery: WebhookDelivery,
        generation: int,
        *,
        error: str,
        status_code: int | None,
    ) -> None:
        """Handle a failed delivery: retry or dead-letter."""
        if delivery.attempt_count >= delivery.max_attempts:
            dead_letter_id = uuid4().hex
            audit_record, audit_repository = self._transition_audit(
                delivery,
                "delivery_dead_lettered",
                dead_letter_id=dead_letter_id,
                upstream_status_code=status_code,
            )
            transitioned_id = await self.repository.dead_letter(
                delivery.delivery_id,
                generation,
                dead_letter_id=dead_letter_id,
                audit_record=audit_record,
                audit_repository=audit_repository,
            )
            if transitioned_id is not None:
                logger.warning(
                    "webhook delivery %s dead-lettered after %d attempts",
                    delivery.delivery_id,
                    delivery.attempt_count,
                )
        else:
            delay = next_retry_delay(
                max(0, delivery.attempt_count - 1),
                self.retry_base_delay,
                self.retry_max_delay,
            )
            audit_record, audit_repository = self._transition_audit(
                delivery,
                "delivery_failed",
                upstream_status_code=status_code,
            )
            await self.repository.mark_failed(
                delivery.delivery_id,
                generation,
                error=error,
                status_code=status_code,
                retry_delay=delay,
                audit_record=audit_record,
                audit_repository=audit_repository,
            )

    def _transition_audit(
        self,
        delivery: WebhookDelivery,
        transition: str,
        *,
        dead_letter_id: str | None = None,
        upstream_status_code: int | None = None,
    ):
        """Build metadata-only audit input before the fenced state mutation."""
        if self.audit_recorder is None:
            return None, None
        identity = webhook_event_identity(delivery.payload_json)
        record = self.audit_recorder.build_webhook_event(
            node_id=f"webhook.delivery.{transition.removeprefix('delivery_').replace('_', '-')}",
            actor=None,
            subscription_id=delivery.subscription_id,
            delivery_id=delivery.delivery_id,
            event_id=delivery.event_id,
            dead_letter_id=dead_letter_id,
            event_type=delivery.event_type.value,
            transition=transition,
            run_id=identity["run_id"],
            approval_id=identity["approval_id"],
            thread_id=identity["thread_id"],
            graph_version_ref=identity["graph_version_ref"],
            attempt=delivery.attempt_count,
            upstream_status_code=upstream_status_code,
        )
        return record, self.audit_recorder.repository


WebhookDeliveryWorker.__signature__ = inspect.signature(WebhookDeliveryWorker).replace(  # type: ignore[attr-defined]
    parameters=[
        parameter
        for name, parameter in inspect.signature(WebhookDeliveryWorker).parameters.items()
        if name != "audit_recorder"
    ]
)
