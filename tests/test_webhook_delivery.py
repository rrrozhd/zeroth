"""Tests for WebhookDeliveryWorker: HTTP delivery, retry, dead-lettering, and backoff."""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from zeroth.service.service_audit import ServiceAuditRecorder


@pytest.fixture
async def webhook_repo(sqlite_db):
    return WebhookRepository.for_default_compatibility(sqlite_db)


@pytest.fixture
def http_client():
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
def webhook_audit_repository():
    repository = AsyncMock()
    repository._signer = object()
    repository.write_in_transaction.side_effect = lambda _connection, record: record.model_copy(
        update={"record_signature": "signed"}
    )
    return repository


@pytest.fixture
def webhook_audit_recorder(webhook_audit_repository, webhook_repo):
    webhook_audit_repository._database = webhook_repo._database
    return ServiceAuditRecorder(
        repository=webhook_audit_repository,
        deployment=SimpleNamespace(
            deployment_ref="deploy-1",
            graph_version_ref="current-graph:v9",
            tenant_id="default",
            workspace_id="workspace-1",
        ),
        require_signed=True,
    )


@pytest.fixture
async def worker(webhook_repo, http_client, webhook_audit_recorder):
    return WebhookDeliveryWorker(
        repository=webhook_repo,
        http_client=http_client,
        audit_recorder=webhook_audit_recorder,
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
        payload_json=(
            '{"event_type":"run.completed","data":{'
            '"run_id":"run-historical",'
            '"thread_id":"thread-historical",'
            '"graph_version_ref":"graph-historical:v3",'
            '"approval_id":"approval-historical",'
            '"ignored_prose":"must never enter audit metadata"}}'
        ),
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

    async def test_successful_delivery_records_signed_historical_audit(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 204
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        written = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert written.node_id == "webhook.delivery.delivered"
        assert written.run_id == "run-historical"
        assert written.thread_id == "thread-historical"
        assert written.graph_version_ref == "graph-historical:v3"
        assert written.execution_metadata == {
            "webhook_subscription_id": sub.subscription_id,
            "webhook_delivery_id": delivery.delivery_id,
            "webhook_event_id": "evt-1",
            "webhook_event_type": "run.completed",
            "webhook_transition": "delivery_delivered",
            "attempt": 1,
            "upstream_status_code": 204,
        }
        assert written.approval_actions[0].approval_id == "approval-historical"
        assert "ignored_prose" not in written.model_dump_json()

    async def test_delivered_state_and_signed_audit_roll_back_together_on_audit_failure(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        _sub, _delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 204
        http_client.post.return_value = response
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        webhook_audit_repository.write_in_transaction.side_effect = RuntimeError(
            "audit insert failed"
        )

        with pytest.raises(RuntimeError, match="audit insert failed"):
            await worker._deliver(claim.delivery, claim.generation)

        persisted = await webhook_repo.get_delivery(claim.delivery.delivery_id)
        assert persisted is not None
        assert persisted.status.value == "delivering"

    async def test_failed_retry_state_rolls_back_when_signed_audit_fails(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        await _create_sub_and_delivery(webhook_repo, max_attempts=3)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        http_client.post.return_value = response
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        webhook_audit_repository.write_in_transaction.side_effect = RuntimeError(
            "retry audit failed"
        )

        with pytest.raises(RuntimeError, match="retry audit failed"):
            await worker._deliver(claim.delivery, claim.generation)

        persisted = await webhook_repo.get_delivery(claim.delivery.delivery_id)
        assert persisted is not None
        assert persisted.status.value == "delivering"

    async def test_dead_letter_and_status_roll_back_when_signed_audit_fails(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        await _create_sub_and_delivery(webhook_repo, max_attempts=1)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        http_client.post.return_value = response
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        webhook_audit_repository.write_in_transaction.side_effect = RuntimeError(
            "dead-letter audit failed"
        )

        with pytest.raises(RuntimeError, match="dead-letter audit failed"):
            await worker._deliver(claim.delivery, claim.generation)

        persisted = await webhook_repo.get_delivery(claim.delivery.delivery_id)
        assert persisted is not None
        assert persisted.status.value == "delivering"
        assert await webhook_repo.list_dead_letters() == []

    async def test_lost_delivery_fence_does_not_record_transition_audit(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        http_client.post.return_value = response
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        worker.repository.mark_delivered = AsyncMock(return_value=False)

        await worker._deliver(claim.delivery, claim.generation)

        webhook_audit_repository.write_in_transaction.assert_not_awaited()

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

    async def test_delivery_pins_ip_and_preserves_host_and_sni(
        self, worker, webhook_repo, http_client, monkeypatch
    ):
        _sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        http_client.post.return_value = response
        monkeypatch.setattr(
            "zeroth.platform.primitives.boundary._resolved_addresses",
            lambda _host: [ipaddress.ip_address("93.184.216.34")],
        )

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        call = http_client.post.call_args
        assert call.args[0] == "https://93.184.216.34/hook"
        assert call.kwargs["headers"]["Host"] == "example.com"
        assert call.kwargs["headers"]["Connection"] == "close"
        assert call.kwargs["follow_redirects"] is False
        assert call.kwargs["extensions"] == {"sni_hostname": "example.com"}

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

    async def test_failed_attempt_records_typed_audit_without_error_text(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        sub, delivery = await _create_sub_and_delivery(webhook_repo)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        written = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert written.execution_metadata == {
            "webhook_subscription_id": sub.subscription_id,
            "webhook_delivery_id": delivery.delivery_id,
            "webhook_event_id": "evt-1",
            "webhook_event_type": "run.completed",
            "webhook_transition": "delivery_failed",
            "attempt": 1,
            "upstream_status_code": 503,
        }
        assert "HTTP 503" not in written.model_dump_json()

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

    async def test_dead_letter_records_only_after_successful_transition(
        self,
        worker,
        webhook_repo,
        http_client,
        webhook_audit_repository,
    ):
        sub, delivery = await _create_sub_and_delivery(
            webhook_repo, attempt_count=4, max_attempts=5
        )
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        http_client.post.return_value = response

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        dead_letter = (await webhook_repo.list_dead_letters())[0]
        written = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert written.execution_metadata == {
            "webhook_subscription_id": sub.subscription_id,
            "webhook_delivery_id": delivery.delivery_id,
            "webhook_event_id": "evt-1",
            "webhook_dead_letter_id": dead_letter.dead_letter_id,
            "webhook_event_type": "run.completed",
            "webhook_transition": "delivery_dead_lettered",
            "attempt": 5,
            "upstream_status_code": 500,
        }

    async def test_internal_target_url_is_never_posted_to(self, worker, webhook_repo, http_client):
        """A02-6 defence in depth: a row persisted before the bound existed.

        Creation-time validation cannot reach a subscription that is already in
        the database, and that row is exactly the SSRF primitive the finding
        describes. The delivery path refuses it rather than trusting storage.
        """
        sub = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="http://169.254.169.254/latest/meta-data/",
            secret="test-secret",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        sub = await webhook_repo.create_subscription(sub)
        await webhook_repo.enqueue_delivery(
            WebhookDelivery(
                subscription_id=sub.subscription_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                event_id="evt-ssrf",
                payload_json='{"event_type":"run.completed","data":{}}',
            )
        )

        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await worker._deliver(claim.delivery, claim.generation)

        http_client.post.assert_not_called()

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
