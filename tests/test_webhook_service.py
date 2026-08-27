"""Tests for WebhookService: event emission, subscription management, dead-letter replay."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository
from zeroth.governance.identity import ActorIdentity, AuthMethod, ServiceRole
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.webhooks.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository
from zeroth.service.webhooks.service import WebhookService
from zeroth.service.service_audit import ServiceAuditRecorder


@pytest.fixture
async def webhook_repo(sqlite_db):
    return WebhookRepository.for_default_compatibility(sqlite_db)


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
async def webhook_service(webhook_repo, webhook_audit_recorder):
    return WebhookService(
        repository=webhook_repo,
        default_max_retries=5,
        audit_recorder=webhook_audit_recorder,
    )


async def _create_sub(
    webhook_repo: WebhookRepository,
    *,
    deployment_ref: str = "deploy-1",
    tenant_id: str = "default",
    event_types: list[WebhookEventType] | None = None,
    active: bool = True,
) -> WebhookSubscription:
    sub = WebhookSubscription(
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        target_url="https://example.com/hook",
        event_types=event_types or [WebhookEventType.RUN_COMPLETED],
        active=active,
    )
    return await webhook_repo.create_subscription(sub)


def _real_audited_service(sqlite_db):
    repository = WebhookRepository.for_default_compatibility(sqlite_db)
    signer = EnvHmacSigner(key_id="webhook-test", keys={"webhook-test": b"test-key"})
    audits = AuditRepository.for_default_compatibility(sqlite_db, signer=signer)
    recorder = ServiceAuditRecorder(
        repository=audits,
        deployment=SimpleNamespace(
            deployment_ref="deploy-1",
            graph_version_ref="graph:v1",
            tenant_id="default",
            workspace_id=None,
        ),
        require_signed=True,
    )
    return repository, audits, WebhookService(repository=repository, audit_recorder=recorder), signer


def _fail_after_audit_insert(audits, monkeypatch):
    original = audits._write_bound

    async def fail_after_audit_insert(bound, record):
        await original(bound, record)
        raise RuntimeError("simulated process failure before commit")

    monkeypatch.setattr(audits, "_write_bound", fail_after_audit_insert)


class TestEmitEvent:
    """WebhookService.emit_event enqueues deliveries for matching subscriptions."""

    async def test_enqueues_delivery_per_matching_subscription(self, webhook_service, webhook_repo):
        sub1 = await _create_sub(webhook_repo, deployment_ref="deploy-1")
        sub2 = await _create_sub(
            webhook_repo,
            deployment_ref="deploy-1",
            event_types=[WebhookEventType.RUN_COMPLETED, WebhookEventType.RUN_FAILED],
        )
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1"},
        )
        assert len(deliveries) == 2
        sub_ids = {d.subscription_id for d in deliveries}
        assert sub_ids == {sub1.subscription_id, sub2.subscription_id}
        for d in deliveries:
            assert d.status == DeliveryStatus.PENDING
            assert d.event_type == WebhookEventType.RUN_COMPLETED

    async def test_no_matching_subscriptions_enqueues_nothing(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="other-deploy")
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1"},
        )
        assert deliveries == []

    async def test_payload_structure(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo)
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "r1", "status": "completed"},
        )
        assert len(deliveries) == 1
        payload = WebhookEventPayload.model_validate_json(deliveries[0].payload_json)
        assert payload.event_type == WebhookEventType.RUN_COMPLETED
        assert payload.deployment_ref == "deploy-1"
        assert payload.tenant_id == "default"
        assert payload.data == {"run_id": "r1", "status": "completed"}
        assert payload.event_id  # non-empty

    async def test_enqueue_audit_preserves_historical_event_identity(
        self,
        webhook_service,
        webhook_repo,
        webhook_audit_repository,
    ):
        sub = await _create_sub(
            webhook_repo,
            event_types=[WebhookEventType.APPROVAL_RESOLVED],
        )

        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.APPROVAL_RESOLVED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={
                "run_id": "run-historical",
                "thread_id": "thread-historical",
                "graph_version_ref": "graph-historical:v3",
                "approval_id": "approval-historical",
                "ignored_prose": "must never enter audit metadata",
            },
        )

        assert len(deliveries) == 1
        written = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert written.record_signature is None
        assert written.node_id == "webhook.delivery.enqueue"
        assert written.run_id == "run-historical"
        assert written.thread_id == "thread-historical"
        assert written.graph_version_ref == "graph-historical:v3"
        assert written.execution_metadata == {
            "webhook_subscription_id": sub.subscription_id,
            "webhook_delivery_id": deliveries[0].delivery_id,
            "webhook_event_id": deliveries[0].event_id,
            "webhook_event_type": "approval.resolved",
            "webhook_transition": "delivery_enqueued",
        }
        assert written.approval_actions[0].approval_id == "approval-historical"
        assert "ignored_prose" not in written.model_dump_json()

    async def test_inactive_subscriptions_excluded(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, active=False)
        deliveries = await webhook_service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={},
        )
        assert deliveries == []

    async def test_enqueue_and_signed_audit_roll_back_together_on_audit_failure(
        self,
        webhook_service,
        webhook_repo,
        webhook_audit_repository,
    ):
        sub = await _create_sub(webhook_repo)
        webhook_audit_repository.write_in_transaction.side_effect = RuntimeError(
            "audit insert failed"
        )

        with pytest.raises(RuntimeError, match="audit insert failed"):
            await webhook_service.emit_event(
                event_type=WebhookEventType.RUN_COMPLETED,
                deployment_ref="deploy-1",
                tenant_id="default",
                data={"run_id": "atomic-run"},
            )

        assert await webhook_repo.list_deliveries(subscription_id=sub.subscription_id) == []

    async def test_fanout_is_all_or_none_when_second_audit_fails(
        self,
        webhook_service,
        webhook_repo,
        webhook_audit_repository,
    ):
        first = await _create_sub(webhook_repo)
        second = await _create_sub(webhook_repo)
        calls = 0

        async def fail_second(_connection, record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second audit failed")
            return record.model_copy(update={"record_signature": "signed"})

        webhook_audit_repository.write_in_transaction.side_effect = fail_second
        with pytest.raises(RuntimeError, match="second audit failed"):
            await webhook_service.emit_event(
                event_type=WebhookEventType.RUN_COMPLETED,
                deployment_ref="deploy-1",
                tenant_id="default",
                data={"run_id": "atomic-fanout-run"},
            )

        assert await webhook_repo.list_deliveries(subscription_id=first.subscription_id) == []
        assert await webhook_repo.list_deliveries(subscription_id=second.subscription_id) == []

    async def test_unsigned_audit_fails_closed_before_delivery_is_visible(
        self,
        webhook_repo,
        webhook_audit_repository,
    ):
        sub = await _create_sub(webhook_repo)
        webhook_audit_repository._signer = None
        webhook_audit_repository._database = webhook_repo._database
        webhook_audit_repository.write_in_transaction.side_effect = lambda _connection, record: (
            record
        )
        recorder = ServiceAuditRecorder(
            repository=webhook_audit_repository,
            deployment=SimpleNamespace(
                deployment_ref="deploy-1",
                graph_version_ref="graph:v1",
                tenant_id="default",
                workspace_id="workspace-1",
            ),
            require_signed=False,
        )
        service = WebhookService(repository=webhook_repo, audit_recorder=recorder)

        with pytest.raises(RuntimeError, match="audit was not signed"):
            await service.emit_event(
                event_type=WebhookEventType.RUN_COMPLETED,
                deployment_ref="deploy-1",
                tenant_id="default",
                data={"run_id": "unsigned-run"},
            )

        assert await webhook_repo.list_deliveries(subscription_id=sub.subscription_id) == []

    async def test_real_signed_audit_and_delivery_share_one_commit(self, sqlite_db):
        repository, audits, service, signer = _real_audited_service(sqlite_db)
        sub = await _create_sub(repository)

        deliveries = await service.emit_event(
            event_type=WebhookEventType.RUN_COMPLETED,
            deployment_ref="deploy-1",
            tenant_id="default",
            data={"run_id": "atomic-signed-run"},
        )

        assert len(await repository.list_deliveries(subscription_id=sub.subscription_id)) == 1
        records = await audits.list_by_run("atomic-signed-run")
        assert len(records) == len(deliveries) == 1
        assert records[0].record_signature is not None
        report = await AuditContinuityVerifier(audits, signer=signer).verify_run(
            "atomic-signed-run"
        )
        assert report.verified
        assert report.signature_verified

    async def test_real_audit_failure_rolls_back_delivery_and_chain_head(
        self, sqlite_db, monkeypatch
    ):
        repository, audits, service, _signer = _real_audited_service(sqlite_db)
        sub = await _create_sub(repository)
        _fail_after_audit_insert(audits, monkeypatch)
        with pytest.raises(RuntimeError, match="before commit"):
            await service.emit_event(
                event_type=WebhookEventType.RUN_COMPLETED,
                deployment_ref="deploy-1",
                tenant_id="default",
                data={"run_id": "atomic-failed-run"},
            )

        assert await repository.list_deliveries(subscription_id=sub.subscription_id) == []
        assert await audits.list_by_run("atomic-failed-run") == []


class TestSubscriptionManagement:
    """WebhookService delegates subscription CRUD to repository."""

    async def test_create_subscription(self, webhook_service, webhook_audit_repository):
        sub = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        actor = ActorIdentity(
            subject="admin-1",
            auth_method=AuthMethod.API_KEY,
            roles=[ServiceRole.ADMIN],
            tenant_id="default",
            workspace_id="workspace-1",
        )
        result = await webhook_service.create_subscription(sub, actor=actor)
        assert result.subscription_id == sub.subscription_id
        audit = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert audit.node_id == "webhook.subscription.create"
        assert audit.actor == actor
        assert audit.execution_metadata["webhook_transition"] == "subscription_created"
        assert "example.com" not in audit.model_dump_json()

    async def test_create_and_signed_audit_roll_back_together_after_audit_insert(
        self, sqlite_db, monkeypatch
    ):
        repository, audits, service, _signer = _real_audited_service(sqlite_db)
        subscription = WebhookSubscription(
            deployment_ref="deploy-1",
            target_url="https://example.com/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
        _fail_after_audit_insert(audits, monkeypatch)

        with pytest.raises(RuntimeError, match="before commit"):
            await service.create_subscription(subscription)

        assert await repository.get_subscription(subscription.subscription_id) is None
        assert await audits.list_by_deployment("deploy-1") == []

    async def test_list_subscriptions(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="deploy-1")
        await _create_sub(webhook_repo, deployment_ref="deploy-2")
        result = await webhook_service.list_subscriptions()
        assert len(result) == 2

    async def test_list_subscriptions_filtered(self, webhook_service, webhook_repo):
        await _create_sub(webhook_repo, deployment_ref="deploy-1")
        await _create_sub(webhook_repo, deployment_ref="deploy-2")
        result = await webhook_service.list_subscriptions(deployment_ref="deploy-1")
        assert len(result) == 1
        assert result[0].deployment_ref == "deploy-1"

    async def test_list_deliveries_scopes_before_limit(self, webhook_service, webhook_repo):
        own = await _create_sub(webhook_repo, deployment_ref="deploy-1")
        foreign = await _create_sub(webhook_repo, deployment_ref="deploy-2")
        await webhook_repo.enqueue_delivery(
            WebhookDelivery(
                delivery_id="foreign-delivery",
                subscription_id=foreign.subscription_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                payload_json="{}",
            )
        )
        await webhook_repo.enqueue_delivery(
            WebhookDelivery(
                delivery_id="own-delivery",
                subscription_id=own.subscription_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                payload_json="{}",
            )
        )

        result = await webhook_service.list_deliveries(
            subscription_ids=[own.subscription_id], limit=1
        )

        assert [delivery.delivery_id for delivery in result] == ["own-delivery"]

    async def test_deactivate_subscription(
        self, webhook_service, webhook_repo, webhook_audit_repository
    ):
        sub = await _create_sub(webhook_repo)
        await webhook_service.deactivate_subscription(sub.subscription_id)
        updated = await webhook_repo.get_subscription(sub.subscription_id)
        assert updated is not None
        assert updated.active is False
        audit = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert audit.node_id == "webhook.subscription.deactivate"
        assert audit.execution_metadata["webhook_transition"] == "subscription_deactivated"

    async def test_deactivate_and_signed_audit_roll_back_together_after_audit_insert(
        self, sqlite_db, monkeypatch
    ):
        repository, audits, service, _signer = _real_audited_service(sqlite_db)
        subscription = await _create_sub(repository)
        _fail_after_audit_insert(audits, monkeypatch)

        with pytest.raises(RuntimeError, match="before commit"):
            await service.deactivate_subscription(subscription.subscription_id)

        persisted = await repository.get_subscription(subscription.subscription_id)
        assert persisted is not None and persisted.active
        assert await audits.list_by_deployment("deploy-1") == []


class TestReplayDeadLetter:
    """WebhookService.replay_dead_letter re-enqueues a dead-letter entry."""

    async def test_replay_creates_new_pending_delivery(self, webhook_service, webhook_repo):
        sub = await _create_sub(webhook_repo)
        # Create a delivery and dead-letter it
        delivery = WebhookDelivery(
            subscription_id=sub.subscription_id,
            event_type=WebhookEventType.RUN_COMPLETED,
            event_id="evt-1",
            payload_json='{"test": true}',
            max_attempts=1,
            attempt_count=1,
        )
        delivery = await webhook_repo.enqueue_delivery(delivery)
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        await webhook_repo.dead_letter(delivery.delivery_id, claim.generation)

        dead_letters = await webhook_repo.list_dead_letters()
        assert len(dead_letters) == 1
        dl = dead_letters[0]

        replayed = await webhook_service.replay_dead_letter(dl.dead_letter_id)
        assert replayed.status == DeliveryStatus.PENDING
        assert replayed.subscription_id == sub.subscription_id
        assert replayed.event_type == WebhookEventType.RUN_COMPLETED
        assert replayed.payload_json == '{"test": true}'
        assert replayed.attempt_count == 0

    async def test_replay_enqueue_records_delivery_transition(
        self,
        webhook_service,
        webhook_repo,
        webhook_audit_repository,
    ):
        sub = await _create_sub(webhook_repo)
        delivery = await webhook_repo.enqueue_delivery(
            WebhookDelivery(
                subscription_id=sub.subscription_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                event_id="evt-replay",
                payload_json=(
                    '{"event_type":"run.completed","data":{'
                    '"run_id":"run-original",'
                    '"graph_version_ref":"graph-original:v1"}}'
                ),
                max_attempts=1,
            )
        )
        claim = await webhook_repo.claim_pending_delivery()
        assert claim is not None
        assert await webhook_repo.dead_letter(delivery.delivery_id, claim.generation)
        dead_letter = (await webhook_repo.list_dead_letters())[0]

        replayed = await webhook_service.replay_dead_letter(dead_letter.dead_letter_id)

        written = webhook_audit_repository.write_in_transaction.await_args.args[1]
        assert written.run_id == "run-original"
        assert written.graph_version_ref == "graph-original:v1"
        assert written.execution_metadata == {
            "webhook_subscription_id": sub.subscription_id,
            "webhook_delivery_id": replayed.delivery_id,
            "webhook_event_id": "evt-replay",
            "webhook_event_type": "run.completed",
            "webhook_transition": "delivery_enqueued",
        }

    async def test_replay_and_signed_enqueue_audit_roll_back_together_after_audit_insert(
        self, sqlite_db, monkeypatch
    ):
        repository, audits, service, _signer = _real_audited_service(sqlite_db)
        subscription = await _create_sub(repository)
        original = await repository.enqueue_delivery(
            WebhookDelivery(
                subscription_id=subscription.subscription_id,
                event_type=WebhookEventType.RUN_COMPLETED,
                event_id="evt-replay-rollback",
                payload_json=(
                    '{"event_type":"run.completed","data":'
                    '{"run_id":"run-replay-rollback"}}'
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
        assert await audits.list_by_run("run-replay-rollback") == []

    async def test_replay_nonexistent_raises_key_error(self, webhook_service):
        with pytest.raises(KeyError):
            await webhook_service.replay_dead_letter("nonexistent-id")
