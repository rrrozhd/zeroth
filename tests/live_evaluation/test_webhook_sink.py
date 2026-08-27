from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from release.live_evaluation.webhook_sink import (
    EVALUATION_WEBHOOK_HOST,
    EvaluationWebhookSink,
    EvaluationWebhookTransport,
)
from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.service import _wire_evaluation_webhook_sink
from zeroth.service.webhooks.models import (
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.signing import sign_payload


def test_concurrent_sink_initialization_is_migration_safe(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-webhook-sink.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE webhook_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                signature_verified INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE webhook_receipts (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                receipt_id TEXT NOT NULL UNIQUE,
                signature_verified INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        sinks = list(executor.map(lambda _index: EvaluationWebhookSink(path), range(32)))

    assert all(sink.path == path for sink in sinks)


class _Repository:
    def __init__(self, delivery: WebhookDelivery, subscription: WebhookSubscription) -> None:
        self.delivery = delivery
        self.subscription = subscription

    async def get_delivery(self, delivery_id: str):
        return self.delivery if delivery_id == self.delivery.delivery_id else None

    async def get_subscription(self, subscription_id: str):
        return self.subscription if subscription_id == self.subscription.subscription_id else None


def _fixture(tmp_path, *, path: str = "success"):
    payload = json.dumps({"event_id": "event-1", "data": {"run_id": "run-1"}})
    subscription = WebhookSubscription(
        subscription_id="subscription-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
        target_url=f"https://{EVALUATION_WEBHOOK_HOST}/zeroth-evaluation/{path}",
        secret="fixture-signing-secret",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )
    delivery = WebhookDelivery(
        delivery_id="delivery-1",
        subscription_id=subscription.subscription_id,
        event_type=WebhookEventType.RUN_COMPLETED,
        event_id="event-1",
        payload_json=payload,
    )
    sink = EvaluationWebhookSink(tmp_path / "webhook-sink.sqlite3")
    transport = EvaluationWebhookTransport(
        repository=_Repository(delivery, subscription),
        sink=sink,
    )
    headers = {
        "Host": EVALUATION_WEBHOOK_HOST,
        "X-Zeroth-Delivery": delivery.delivery_id,
        "X-Zeroth-Event": delivery.event_type.value,
        "X-Zeroth-Signature": f"sha256={sign_payload(payload.encode(), subscription.secret)}",
    }
    request = httpx.Request(
        "POST",
        f"https://203.0.113.7/zeroth-evaluation/{path}",
        headers=headers,
        content=payload,
    )
    return sink, transport, request


def test_existing_event_correlated_sink_migrates_subscription_scope(tmp_path) -> None:
    path = tmp_path / "legacy-webhook-sink.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE webhook_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                event_id TEXT,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                signature_verified INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE webhook_receipts (
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                receipt_id TEXT NOT NULL UNIQUE,
                signature_verified INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX ux_webhook_receipts_event_id
                ON webhook_receipts (event_id) WHERE event_id IS NOT NULL;
            """
        )

    EvaluationWebhookSink(path)

    with sqlite3.connect(path) as connection:
        for table in ("webhook_attempts", "webhook_receipts"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert "subscription_id" in columns
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(webhook_receipts)")
        }
    assert "ux_webhook_receipts_event_id" not in indexes
    assert "ux_webhook_receipts_subscription_event" in indexes


@pytest.mark.asyncio
async def test_signed_success_is_durable_and_duplicate_safe(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path)

    first = await transport.handle_async_request(request)
    second = await transport.handle_async_request(request)

    assert first.status_code == 204
    assert second.status_code == 204
    assert second.headers["X-Zeroth-Evaluation-Duplicate"] == "true"
    receipts = EvaluationWebhookSink(sink.path).receipts()
    assert len(receipts) == 1
    assert receipts[0]["delivery_id"] == "delivery-1"
    assert receipts[0]["signature_verified"] is True
    assert receipts[0]["attempt_count"] == 2
    assert "payload" not in receipts[0]


@pytest.mark.asyncio
async def test_same_event_replayed_with_new_delivery_id_returns_prior_receipt(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path)

    first = await transport.handle_async_request(request)
    transport.repository.delivery = transport.repository.delivery.model_copy(
        update={"delivery_id": "delivery-replay"}
    )
    request.headers["X-Zeroth-Delivery"] = "delivery-replay"
    replay = await transport.handle_async_request(request)

    assert first.status_code == 204
    assert replay.status_code == 204
    assert replay.headers["X-Zeroth-Evaluation-Duplicate"] == "true"
    receipts = sink.receipts()
    assert len(receipts) == 1
    assert receipts[0]["event_id"] == "event-1"
    assert receipts[0]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_same_event_for_distinct_subscriptions_creates_distinct_receipts(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path)

    first = await transport.handle_async_request(request)
    second_subscription = transport.repository.subscription.model_copy(
        update={"subscription_id": "subscription-2", "secret": "fixture-secret-2"}
    )
    transport.repository.subscription = second_subscription
    transport.repository.delivery = transport.repository.delivery.model_copy(
        update={"delivery_id": "delivery-2", "subscription_id": "subscription-2"}
    )
    request.headers["X-Zeroth-Delivery"] = "delivery-2"
    request.headers["X-Zeroth-Signature"] = (
        "sha256=" + sign_payload(request.content, second_subscription.secret)
    )

    second = await transport.handle_async_request(request)

    assert first.status_code == 204
    assert second.status_code == 204
    assert second.headers["X-Zeroth-Evaluation-Duplicate"] == "false"
    receipts = sink.receipts()
    assert len(receipts) == 2
    assert {row["subscription_id"] for row in receipts} == {
        "subscription-1",
        "subscription-2",
    }


@pytest.mark.asyncio
async def test_invalid_signature_is_refused_without_receipt(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path)
    request.headers["X-Zeroth-Signature"] = "sha256=invalid"

    response = await transport.handle_async_request(request)

    assert response.status_code == 401
    assert sink.receipts() == []


@pytest.mark.asyncio
async def test_controlled_non_2xx_and_timeout_hooks_do_not_create_receipts(tmp_path) -> None:
    failure_sink, failure_transport, failure_request = _fixture(tmp_path, path="non2xx")
    assert (await failure_transport.handle_async_request(failure_request)).status_code == 503
    assert failure_sink.receipts() == []

    timeout_sink, timeout_transport, timeout_request = _fixture(tmp_path, path="timeout")
    with pytest.raises(httpx.ReadTimeout):
        await timeout_transport.handle_async_request(timeout_request)
    assert timeout_sink.receipts() == []


@pytest.mark.asyncio
async def test_timeout_after_commit_retries_without_a_second_side_effect(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path, path="timeout-after-commit/scenario-1")

    with pytest.raises(httpx.ReadTimeout):
        await transport.handle_async_request(request)
    retry = await transport.handle_async_request(request)

    assert retry.status_code == 204
    assert retry.headers["X-Zeroth-Evaluation-Duplicate"] == "true"
    receipts = sink.receipts()
    assert len(receipts) == 1
    assert receipts[0]["event_id"] == "event-1"
    assert receipts[0]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_unavailable_sink_hook_fails_without_a_receipt(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path, path="unavailable")

    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(request)

    assert sink.receipts() == []


@pytest.mark.asyncio
async def test_flaky_hook_fails_five_attempts_then_accepts_replay(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path, path="flaky/scenario-1")

    for _attempt in range(5):
        assert (await transport.handle_async_request(request)).status_code == 503
    replay = await transport.handle_async_request(request)

    assert replay.status_code == 204
    assert len(sink.receipts()) == 1


@pytest.mark.asyncio
async def test_restart_after_lease_hook_blocks_once_then_recovers_from_durable_marker(
    tmp_path,
) -> None:
    sink, transport, request = _fixture(tmp_path, path="restart-after-lease/scenario-1")

    first_attempt = asyncio.create_task(transport.handle_async_request(request))
    await asyncio.wait_for(
        sink.wait_for_outcome("leased_before_restart:scenario-1"), timeout=1
    )
    assert not first_attempt.done()

    # Cancelling the task models the worker process being terminated while its
    # durable delivery row remains leased.  A fresh transport is the restarted
    # process; it must observe the marker and complete the retried generation.
    first_attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_attempt
    restarted_sink = EvaluationWebhookSink(sink.path)
    restarted_transport = EvaluationWebhookTransport(
        repository=transport.repository,
        sink=restarted_sink,
    )

    recovered = await restarted_transport.handle_async_request(request)

    assert recovered.status_code == 204
    receipts = restarted_sink.receipts()
    assert len(receipts) == 1
    assert receipts[0]["event_id"] == "event-1"
    assert receipts[0]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_transport_fails_closed_for_non_campaign_destination(tmp_path) -> None:
    sink, transport, request = _fixture(tmp_path)
    request.headers["Host"] = "example.org"

    response = await transport.handle_async_request(request)

    assert response.status_code == 421
    assert sink.receipts() == []


@pytest.mark.asyncio
async def test_evaluation_bootstrap_replaces_network_client_and_closes_prior_client(tmp_path) -> None:
    sink, _transport, _request = _fixture(tmp_path)
    repository = _Repository(
        WebhookDelivery(
            delivery_id="delivery-2",
            subscription_id="subscription-2",
            event_type=WebhookEventType.RUN_COMPLETED,
            payload_json="{}",
        ),
        WebhookSubscription(
            subscription_id="subscription-2",
            deployment_ref="deployment-1",
            tenant_id="evaluation-tenant-1",
            target_url=f"https://{EVALUATION_WEBHOOK_HOST}/zeroth-evaluation/success",
            event_types=[WebhookEventType.RUN_COMPLETED],
        ),
    )
    prior = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    worker = SimpleNamespace(http_client=prior)
    bootstrap = SimpleNamespace(
        delivery_worker=worker,
        webhook_repository=repository,
        webhook_http_client=prior,
        evaluation_webhook_sink=None,
    )
    campaign = CampaignConfig(
        schema_version=1,
        campaign_id="evaluation-webhook-test",
        tenant_id="evaluation-tenant-1",
        provider="openai",
        model="openai/gpt-4o-mini",
        embedding_model="openai/text-embedding-3-small",
        vector_backend="chroma",
        campaign_budget_usd=Decimal("10"),
        per_run_cap_usd=Decimal("0.25"),
        provider_secret_ref="openai.project",
        artifact_root=tmp_path,
        action_sink_root=tmp_path / "actions",
    )

    await _wire_evaluation_webhook_sink(bootstrap, campaign=campaign)

    assert bootstrap.webhook_http_client is worker.http_client
    assert isinstance(bootstrap.evaluation_webhook_sink, EvaluationWebhookSink)
    assert bootstrap.evaluation_webhook_sink.path == tmp_path / "webhook-sink.sqlite3"
    with pytest.raises(RuntimeError, match="Cannot send a request"):
        await prior.get("https://example.com")
    await worker.http_client.aclose()
