"""Tests for WebhookDeliveryWorker: HTTP delivery, retry, dead-lettering, and backoff."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from zeroth.service.webhooks.delivery import WebhookDeliveryWorker, next_retry_delay
from zeroth.service.webhooks.models import (
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository


@pytest.fixture
async def webhook_repo(sqlite_db):
    return WebhookRepository.for_default_compatibility(sqlite_db)


@pytest.fixture
def http_client():
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
async def worker(webhook_repo, http_client):
    return WebhookDeliveryWorker(
        repository=webhook_repo,
        http_client=http_client,
        poll_interval=0.01,
        retry_base_delay=1.0,
        retry_max_delay=300.0,
    )


async def _create_sub_and_delivery(
    webhook_repo: WebhookRepository,
    *,
    attempt_count: int = 0,
    max_attempts: int = 5,
) -> tuple[WebhookSubscription, WebhookDelivery]:
    sub = WebhookSubscription(
        deployment_ref="deploy-1",
        target_url="https://example.com/hook",
        secret="test-secret",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )
    sub = await webhook_repo.create_subscription(sub)
    delivery = WebhookDelivery(
        subscription_id=sub.subscription_id,
        event_type=WebhookEventType.RUN_COMPLETED,
        event_id="evt-1",
        payload_json='{"event_type":"run.completed","data":{}}',
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    delivery = await webhook_repo.enqueue_delivery(delivery)
    return sub, delivery


class TestDeliver:
    """WebhookDeliveryWorker._deliver handles HTTP responses correctly."""

    async def test_successful_delivery_marks_delivered(self, worker, webhook_repo, http_client):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        http_client.post.assert_called_once()
        call_kwargs = http_client.post.call_args
        assert (
            call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {})).get("Content-Type")
            == "application/json"
        )
        assert "X-Zeroth-Signature" in call_kwargs.kwargs.get(
            "headers", call_kwargs[1].get("headers", {})
        )

    async def test_signature_header_format(self, worker, webhook_repo, http_client):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        call_args = http_client.post.call_args
        headers = call_args.kwargs.get("headers", call_args[1].get("headers", {}))
        sig = headers["X-Zeroth-Signature"]
        assert sig.startswith("sha256=")

    async def test_500_response_calls_mark_failed(self, worker, webhook_repo, http_client):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        # Delivery should be marked as failed (status updated in DB)
        # The repository mark_failed increments attempt_count
        # We verify by checking no dead-letter was created (attempt < max)
        dead_letters = await webhook_repo.list_dead_letters()
        assert len(dead_letters) == 0

    async def test_max_retries_exhausted_dead_letters(self, worker, webhook_repo, http_client):
        sub, delivery = await _create_sub_and_delivery(
            webhook_repo, attempt_count=4, max_attempts=5
        )
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        dead_letters = await webhook_repo.list_dead_letters()
        assert len(dead_letters) == 1
        assert dead_letters[0].delivery_id == delivery.delivery_id

    async def test_timeout_calls_mark_failed(self, worker, webhook_repo, http_client):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        http_client.post.side_effect = httpx.TimeoutException("timed out")

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        dead_letters = await webhook_repo.list_dead_letters()
        assert len(dead_letters) == 0  # not dead-lettered yet, just failed


class TestPollLoop:
    """WebhookDeliveryWorker.poll_loop behaviour."""

    async def test_sleeps_when_no_pending(self, worker, webhook_repo):
        """Poll loop should sleep when no deliveries are pending."""
        sleep_calls = []

        async def mock_sleep(duration):
            sleep_calls.append(duration)
            raise asyncio.CancelledError()  # stop the loop

        with patch("asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(asyncio.CancelledError):
                await worker.poll_loop()

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] == worker.poll_interval


class TestNextRetryDelay:
    """next_retry_delay returns jittered exponential backoff."""

    def test_delay_within_bounds(self):
        for attempt in range(10):
            delay = next_retry_delay(attempt, base=1.0, max_delay=300.0)
            expected_max = min(1.0 * (2**attempt), 300.0)
            assert 0 <= delay <= expected_max

    def test_max_delay_cap(self):
        delay = next_retry_delay(100, base=1.0, max_delay=300.0)
        assert delay <= 300.0

    def test_zero_attempt(self):
        delay = next_retry_delay(0, base=1.0, max_delay=300.0)
        assert 0 <= delay <= 1.0


class TestRetryBackoffWindow:
    """Backoff is computed exactly once across the worker -> repository path.

    Regression: the worker derived a fully-jittered delay via ``next_retry_delay``
    and ``mark_failed`` then re-applied exponential growth + jitter on top, so the
    scheduled retry landed far outside the intended window (roughly
    ``window * 2**(attempt+1)`` seconds out). The worker now owns the policy and
    ``mark_failed`` persists the supplied delay verbatim, so ``next_attempt_at``
    falls inside a single backoff window.
    """

    @pytest.mark.parametrize("attempt_count", [0, 3])
    async def test_next_attempt_within_single_backoff_window(
        self, worker, webhook_repo, http_client, sqlite_db, attempt_count
    ):
        sub, delivery = await _create_sub_and_delivery(webhook_repo, attempt_count=attempt_count)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        http_client.post.return_value = response

        # The single jittered backoff window for this attempt count.
        window = min(worker.retry_base_delay * (2**attempt_count), worker.retry_max_delay)

        # Pin jitter to its upper bound so the schedule is deterministic and the
        # assertion checks the top edge of the window -- exactly where the old
        # double-compute overshot.
        before = datetime.now(UTC)
        with patch("zeroth.service.webhooks.delivery.random.uniform", lambda _low, high: high):
            claim = await webhook_repo.claim_pending_delivery()
            assert claim is not None
            await worker._deliver(claim.delivery, claim.generation)
        after = datetime.now(UTC)

        async with sqlite_db.transaction() as conn:
            row = await conn.fetch_one(
                "SELECT next_attempt_at, attempt_count, status "
                "FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            )
        next_at = datetime.fromisoformat(row["next_attempt_at"])

        # next_attempt_at must sit within [before, after + window]; the double
        # exponentiation bug would push it to ~window * 2**(attempt+1) seconds out.
        assert before <= next_at <= after + timedelta(seconds=window)
        assert row["attempt_count"] == attempt_count + 1
        assert row["status"] == "failed"
