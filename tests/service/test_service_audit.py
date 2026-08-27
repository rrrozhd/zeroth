from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.service_audit import ServiceAuditRecorder


def _deployment() -> SimpleNamespace:
    return SimpleNamespace(
        deployment_ref="deploy-1",
        graph_version_ref="graph:v1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
    )


async def test_webhook_service_event_is_signed_correlated_and_contains_no_target_url() -> None:
    repository = AsyncMock()
    repository._signer = object()
    repository.write.side_effect = lambda record: record.model_copy(
        update={"record_signature": "signed"}
    )
    recorder = ServiceAuditRecorder(
        repository=repository,
        deployment=_deployment(),
        require_signed=True,
    )

    result = await recorder.record_webhook_event(
        node_id="webhook.dead-letter.replay",
        actor=ActorIdentity(subject="admin-1", auth_method=AuthMethod.API_KEY),
        subscription_id="sub-1",
        delivery_id="delivery-1",
        event_id="event-1",
        dead_letter_id="dead-1",
        event_type="approval.resolved",
        transition="replay_authorized",
        run_id="run-1",
        approval_id="approval-1",
        target_url="https://example.com/hooks/private-path",
    )

    assert result.record_signature == "signed"
    assert result.run_id == "run-1"
    assert result.execution_metadata == {
        "webhook_subscription_id": "sub-1",
        "webhook_delivery_id": "delivery-1",
        "webhook_event_id": "event-1",
        "webhook_dead_letter_id": "dead-1",
        "webhook_event_type": "approval.resolved",
        "webhook_transition": "replay_authorized",
        "target_url_sha256": "5eb1c53a55b6428082ab0771b69d276ddf8a5e3532443dbd13c4895d862e8bf4",
    }
    assert result.approval_actions[0].approval_id == "approval-1"
    assert "example.com" not in result.model_dump_json()


async def test_webhook_service_event_fails_before_write_without_signing() -> None:
    repository = AsyncMock()
    repository._signer = None
    recorder = ServiceAuditRecorder(
        repository=repository,
        deployment=_deployment(),
        require_signed=True,
    )

    with pytest.raises(RuntimeError, match="requires audit signing"):
        await recorder.record_webhook_event(
            node_id="webhook.subscription.create",
            actor=None,
            subscription_id="sub-1",
            transition="create_authorized",
        )

    repository.write.assert_not_awaited()


async def test_template_control_plane_event_is_signed_and_metadata_only() -> None:
    repository = AsyncMock()
    repository._signer = object()
    repository.write.side_effect = lambda record: record.model_copy(
        update={"record_signature": "signed"}
    )
    recorder = ServiceAuditRecorder(
        repository=repository,
        deployment=_deployment(),
        require_signed=True,
    )
    actor = ActorIdentity(subject="admin-1", auth_method=AuthMethod.API_KEY)

    result = await recorder.record_template_event(
        actor=actor,
        template_name="grounded-answer",
        template_version=7,
        transition="created",
    )

    assert result.record_signature == "signed"
    assert result.tenant_id == "tenant-1"
    assert result.workspace_id == "workspace-1"
    assert result.actor == actor
    assert result.execution_metadata == {
        "template_name_sha256": hashlib.sha256(b"grounded-answer").hexdigest(),
        "template_version": 7,
        "template_transition": "created",
    }
    assert "grounded-answer" not in result.run_id
    serialized = result.model_dump_json()
    assert "template_str" not in serialized
    assert "variables" not in serialized
    assert "secret" not in serialized


async def test_run_control_event_is_signed_and_correlated_without_payload() -> None:
    repository = AsyncMock()
    repository._signer = object()
    repository.write.side_effect = lambda record: record.model_copy(
        update={"record_signature": "signed"}
    )
    recorder = ServiceAuditRecorder(
        repository=repository,
        deployment=_deployment(),
        require_signed=True,
    )
    actor = ActorIdentity(subject="admin-1", auth_method=AuthMethod.API_KEY)
    run = SimpleNamespace(
        run_id="run-1",
        thread_id="thread-1",
        graph_version_ref="historical-graph:v2",
        deployment_ref="historical-deployment",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
    )

    result = await recorder.record_run_control_event(
        actor=actor,
        run=run,
        transition="cancelled",
        descendant_count=4,
    )

    assert result.record_signature == "signed"
    assert result.run_id == "run-1"
    assert result.thread_id == "thread-1"
    assert result.graph_version_ref == "historical-graph:v2"
    assert result.deployment_ref == "historical-deployment"
    assert result.node_id == "run.control.cancelled"
    assert result.actor == actor
    assert result.execution_metadata == {
        "run_control_transition": "cancelled",
        "descendant_cancellation_count": 4,
    }
    serialized = result.model_dump_json()
    assert "input_payload" not in serialized
    assert "authorization" not in serialized.lower()


async def test_unsigned_local_posture_still_records_neutral_audit() -> None:
    repository = AsyncMock()
    repository._signer = None
    repository.write.side_effect = lambda record: record
    recorder = ServiceAuditRecorder(repository=repository, deployment=_deployment())

    result = await recorder.record_webhook_event(
        node_id="webhook.subscription.create",
        actor=None,
        subscription_id="sub-1",
        transition="create_authorized",
    )

    assert result.record_signature is None
    repository.write.assert_awaited_once()


async def test_webhook_delivery_transitions_form_a_verified_signed_chain(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="webhook-worker", keys={"webhook-worker": b"test-key"})
    repository = AuditRepository.for_default_compatibility(sqlite_db, signer=signer)
    recorder = ServiceAuditRecorder(
        repository=repository,
        deployment=SimpleNamespace(
            deployment_ref="deploy-1",
            graph_version_ref="current-graph:v9",
            tenant_id="default",
            workspace_id=None,
        ),
        require_signed=True,
    )

    await recorder.record_webhook_event(
        node_id="webhook.delivery.enqueue",
        actor=None,
        subscription_id="sub-1",
        delivery_id="delivery-1",
        event_id="event-1",
        event_type="run.completed",
        transition="delivery_enqueued",
        run_id="run-1",
        graph_version_ref="historical-graph:v1",
    )
    await recorder.record_webhook_event(
        node_id="webhook.delivery.delivered",
        actor=None,
        subscription_id="sub-1",
        delivery_id="delivery-1",
        event_id="event-1",
        event_type="run.completed",
        transition="delivery_delivered",
        run_id="run-1",
        graph_version_ref="historical-graph:v1",
        attempt=1,
        upstream_status_code=204,
    )

    report = await AuditContinuityVerifier(repository, signer=signer).verify_run("run-1")
    assert report.verified is True
    assert report.signature_verified is True
    assert report.unsigned_record_count == 0
    assert report.record_count == 2
