"""Provider-free approval webhook lifecycle against durable local adapters."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from release.live_evaluation.webhook_sink import (
    EvaluationWebhookSink,
    EvaluationWebhookTransport,
)
from zeroth.contracts.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.storage import ScopeContext
from zeroth.runtime.runs import Run
from zeroth.service.service_audit import ServiceAuditRecorder
from zeroth.service.webhooks.delivery import WebhookDeliveryWorker
from zeroth.service.webhooks.models import WebhookEventType, WebhookSubscription
from zeroth.service.webhooks.repository import WebhookRepository
from zeroth.service.webhooks.service import WebhookService


def _node(graph_version_ref: str, *, escalation_action: str | None = None) -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval",
        graph_version_ref=graph_version_ref,
        human_approval=HumanApprovalNodeData(
            approval_policy_config={"allow_edits": False},
            sla_timeout_seconds=1 if escalation_action else None,
            escalation_action=escalation_action,
        ),
    )


async def _drain(worker: WebhookDeliveryWorker, repository: WebhookRepository) -> None:
    while claim := await repository.claim_pending_delivery():
        await worker._deliver(claim.delivery, claim.generation)  # noqa: SLF001


async def test_approval_events_are_unique_correlated_signed_and_local_only(
    async_database,
    tmp_path,
    monkeypatch,
) -> None:
    tenant_id = "evaluation-approval-webhooks"
    deployment_ref = "evaluation-approval-webhooks-v1"
    graph_version_ref = "evaluation-approval-webhooks@1"
    scope = ScopeContext(tenant_id=tenant_id, workspace_id="campaign")
    deployment = SimpleNamespace(
        deployment_ref=deployment_ref,
        graph_version_ref=graph_version_ref,
        tenant_id=tenant_id,
        workspace_id="campaign",
    )
    signer = EnvHmacSigner(key_id="approval-webhooks", keys={"approval-webhooks": b"local"})
    audit_repository = AuditRepository.scoped(async_database, scope, signer=signer)
    webhook_repository = WebhookRepository(async_database, scope)
    recorder = ServiceAuditRecorder(
        repository=audit_repository,
        deployment=deployment,
        require_signed=True,
    )
    webhook_service = WebhookService(
        repository=webhook_repository,
        audit_recorder=recorder,
    )
    subscription = await webhook_service.create_subscription(
        WebhookSubscription(
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
            target_url="https://example.com/zeroth-evaluation/success",
            event_types=[
                WebhookEventType.APPROVAL_REQUESTED,
                WebhookEventType.APPROVAL_RESOLVED,
                WebhookEventType.APPROVAL_ESCALATED,
            ],
        )
    )
    run_repository = RunRepository(async_database, scope)
    approval_repository = ApprovalRepository.scoped_for_deployment(
        async_database,
        scope,
        deployment_ref,
    )
    approval_service = ApprovalService(
        repository=approval_repository,
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    approval_service.webhook_service = webhook_service

    resolved_run = Run(
        run_id="approval-webhook-resolved-run",
        thread_id="approval-webhook-resolved-thread",
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        workspace_id="campaign",
    )
    escalated_run = Run(
        run_id="approval-webhook-escalated-run",
        thread_id="approval-webhook-escalated-thread",
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        workspace_id="campaign",
    )
    await run_repository.put(resolved_run)
    await run_repository.put(escalated_run)

    resolved = await approval_service.create_pending(
        run=resolved_run,
        node=_node(graph_version_ref),
        input_payload={"fixture": "resolved"},
    )
    await approval_service.resolve(
        resolved.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(
            subject="reviewer",
            auth_method=AuthMethod.API_KEY,
            tenant_id=tenant_id,
            workspace_id="campaign",
        ),
        tenant_id=tenant_id,
        workspace_id="campaign",
        deployment_ref=deployment_ref,
        graph_version_ref=graph_version_ref,
    )
    # Repeating the exact decision is an idempotent no-op, not a second event.
    await approval_service.resolve(
        resolved.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(
            subject="reviewer",
            auth_method=AuthMethod.API_KEY,
            tenant_id=tenant_id,
            workspace_id="campaign",
        ),
        tenant_id=tenant_id,
        workspace_id="campaign",
        deployment_ref=deployment_ref,
        graph_version_ref=graph_version_ref,
    )

    escalated = await approval_service.create_pending(
        run=escalated_run,
        node=_node(graph_version_ref, escalation_action="alert"),
        input_payload={"fixture": "escalated"},
    )
    escalated.sla_deadline = datetime.now(UTC) - timedelta(seconds=1)
    await approval_repository.write(escalated)
    first, second = await asyncio.gather(
        approval_service.escalate(
            escalated.approval_id,
            tenant_id=tenant_id,
            workspace_id="campaign",
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        ),
        approval_service.escalate(
            escalated.approval_id,
            tenant_id=tenant_id,
            workspace_id="campaign",
            deployment_ref=deployment_ref,
            graph_version_ref=graph_version_ref,
        ),
    )
    assert first.status == second.status

    # No DNS or socket traffic is permitted. Resolution remains exercised, but
    # the resulting IP is handled entirely by EvaluationWebhookTransport.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    sink = EvaluationWebhookSink(tmp_path / "webhook-sink.sqlite3")
    client = httpx.AsyncClient(
        transport=EvaluationWebhookTransport(repository=webhook_repository, sink=sink)
    )
    worker = WebhookDeliveryWorker(
        repository=webhook_repository,
        http_client=client,
        audit_recorder=recorder,
    )
    try:
        await _drain(worker, webhook_repository)
    finally:
        await client.aclose()

    deliveries = await webhook_repository.list_deliveries(
        subscription_id=subscription.subscription_id,
        limit=20,
    )
    projected: list[tuple[str, str, str, str]] = []
    for delivery in deliveries:
        payload = json.loads(delivery.payload_json)
        projected.append(
            (
                delivery.event_type.value,
                payload["data"]["approval_id"],
                payload["data"]["run_id"],
                payload["data"]["thread_id"],
            )
        )
    assert sorted(projected) == sorted(
        [
            (
                "approval.requested",
                resolved.approval_id,
                resolved_run.run_id,
                resolved_run.thread_id,
            ),
            (
                "approval.resolved",
                resolved.approval_id,
                resolved_run.run_id,
                resolved_run.thread_id,
            ),
            (
                "approval.requested",
                escalated.approval_id,
                escalated_run.run_id,
                escalated_run.thread_id,
            ),
            (
                "approval.escalated",
                escalated.approval_id,
                escalated_run.run_id,
                escalated_run.thread_id,
            ),
        ]
    )
    assert len({delivery.event_id for delivery in deliveries}) == 4
    assert len(sink.receipts()) == 4

    for run_id in (resolved_run.run_id, escalated_run.run_id):
        report = await AuditContinuityVerifier(audit_repository, signer=signer).verify_run(
            run_id,
            tenant_id=tenant_id,
            workspace_id="campaign",
            workspace_scoped=True,
            deployment_ref=deployment_ref,
        )
        assert report.verified is True
        assert report.signature_verified is True
        assert report.unsigned_record_count == 0
