"""PostgreSQL transaction proofs for webhook state and signed audit rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import requires_docker
from zeroth.governance.audit import AuditRepository
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.service_audit import ServiceAuditRecorder
from zeroth.service.webhooks.models import (
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository
from zeroth.service.webhooks.service import WebhookService


def _audited_service(database):
    repository = WebhookRepository.for_default_compatibility(database)
    signer = EnvHmacSigner(
        key_id="postgres-webhook-test",
        keys={"postgres-webhook-test": b"postgres-webhook-test-key"},
    )
    audits = AuditRepository.for_default_compatibility(database, signer=signer)
    recorder = ServiceAuditRecorder(
        repository=audits,
        deployment=SimpleNamespace(
            deployment_ref="postgres-webhook-atomicity",
            graph_version_ref="postgres-webhook-atomicity:v1",
            tenant_id="default",
            workspace_id=None,
        ),
        require_signed=True,
    )
    return repository, audits, WebhookService(repository=repository, audit_recorder=recorder)


def _fail_after_audit_insert(audits, monkeypatch: pytest.MonkeyPatch) -> None:
    original = audits._write_bound

    async def fail_after_audit_insert(bound, record):
        await original(bound, record)
        raise RuntimeError("simulated process failure before commit")

    monkeypatch.setattr(audits, "_write_bound", fail_after_audit_insert)


async def _assert_no_audit_state(database, audits) -> None:
    assert await audits.list_by_deployment("postgres-webhook-atomicity") == []
    async with database.transaction() as connection:
        rows = await connection.fetch_all(
            "SELECT run_id FROM audit_chain_heads WHERE tenant_id = ?",
            ("default",),
        )
    assert rows == []


@requires_docker
@pytest.mark.postgres
async def test_postgres_create_and_signed_audit_roll_back_after_audit_insert(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, audits, service = _audited_service(postgres_database)
    subscription = WebhookSubscription(
        deployment_ref="postgres-webhook-atomicity",
        target_url="https://example.invalid/hook",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )
    _fail_after_audit_insert(audits, monkeypatch)

    with pytest.raises(RuntimeError, match="before commit"):
        await service.create_subscription(subscription)

    assert await repository.get_subscription(subscription.subscription_id) is None
    await _assert_no_audit_state(postgres_database, audits)


@requires_docker
@pytest.mark.postgres
async def test_postgres_deactivate_and_signed_audit_roll_back_after_audit_insert(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, audits, service = _audited_service(postgres_database)
    subscription = WebhookSubscription(
        deployment_ref="postgres-webhook-atomicity",
        target_url="https://example.invalid/hook",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )
    await repository.create_subscription(subscription)
    _fail_after_audit_insert(audits, monkeypatch)

    with pytest.raises(RuntimeError, match="before commit"):
        await service.deactivate_subscription(subscription.subscription_id)

    persisted = await repository.get_subscription(subscription.subscription_id)
    assert persisted is not None and persisted.active
    await _assert_no_audit_state(postgres_database, audits)


@requires_docker
@pytest.mark.postgres
async def test_postgres_replay_and_signed_audit_roll_back_after_audit_insert(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, audits, service = _audited_service(postgres_database)
    subscription = WebhookSubscription(
        deployment_ref="postgres-webhook-atomicity",
        target_url="https://example.invalid/hook",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )
    await repository.create_subscription(subscription)
    original = await repository.enqueue_delivery(
        WebhookDelivery(
            subscription_id=subscription.subscription_id,
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="postgres-replay-rollback-event",
            payload_json=(
                '{"event_type":"run.completed","data":'
                '{"run_id":"postgres-replay-rollback"}}'
            ),
            max_attempts=1,
        )
    )
    claim = await repository.claim_pending_delivery()
    assert claim is not None
    assert await repository.dead_letter(original.delivery_id, claim.generation)
    dead_letter = (await repository.list_dead_letters())[0]
    _fail_after_audit_insert(audits, monkeypatch)

    with pytest.raises(RuntimeError, match="before commit"):
        await service.replay_dead_letter(dead_letter.dead_letter_id)

    deliveries = await repository.list_deliveries(
        subscription_id=subscription.subscription_id
    )
    assert [delivery.delivery_id for delivery in deliveries] == [original.delivery_id]
    await _assert_no_audit_state(postgres_database, audits)
