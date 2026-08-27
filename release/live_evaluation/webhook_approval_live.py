"""Create and verify provider-free approval webhook fixtures in a live dev state root."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from zeroth.contracts.graph import HumanApprovalNode, HumanApprovalNodeData
from zeroth.governance.approvals import ApprovalDecision
from zeroth.governance.audit import AuditContinuityVerifier
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.runtime.runs import Run
from zeroth.service.webhooks.models import WebhookEventType, WebhookSubscription

from .config import CampaignConfig
from .service import bootstrap_evaluation_action_service


def _node(graph_version_ref: str, *, escalation_action: str | None = None) -> HumanApprovalNode:
    return HumanApprovalNode(
        node_id="approval-webhook-fixture",
        graph_version_ref=graph_version_ref,
        human_approval=HumanApprovalNodeData(
            approval_policy_config={"allow_edits": False},
            sla_timeout_seconds=1 if escalation_action else None,
            escalation_action=escalation_action,
        ),
    )


def _event_identity(payload_json: str) -> tuple[str, str, str, str]:
    payload = json.loads(payload_json)
    data = payload["data"]
    return (
        payload["event_type"],
        data["approval_id"],
        data["run_id"],
        data["thread_id"],
    )


async def _wait_for_deliveries(bootstrap, subscription_id: str, *, count: int):
    for _ in range(200):
        deliveries = await bootstrap.webhook_repository.list_deliveries(
            subscription_id=subscription_id,
            limit=20,
        )
        if len(deliveries) == count and all(
            item.status.value == "delivered" for item in deliveries
        ):
            return deliveries
        await asyncio.sleep(0.1)
    raise RuntimeError("approval webhook deliveries did not reconcile before the deadline")


async def execute(*, campaign: CampaignConfig, deployment_ref: str) -> dict[str, object]:
    """Run two isolated approval lifecycles and return secret-free evidence."""
    from zeroth.platform.config.settings import get_settings
    from zeroth.platform.storage.factory import create_database

    database = await create_database(get_settings())
    bootstrap = await bootstrap_evaluation_action_service(
        database,
        campaign=campaign,
        deployment_ref=deployment_ref,
        enable_durable_worker=False,
    )
    suffix = uuid4().hex[:12]
    subscription = await bootstrap.webhook_service.create_subscription(
        WebhookSubscription(
            deployment_ref=deployment_ref,
            tenant_id=campaign.tenant_id,
            target_url="https://example.com/zeroth-evaluation/success",
            event_types=[
                WebhookEventType.APPROVAL_REQUESTED,
                WebhookEventType.APPROVAL_RESOLVED,
                WebhookEventType.APPROVAL_ESCALATED,
            ],
        )
    )
    approval_service = bootstrap.approval_service
    graph_version_ref = bootstrap.deployment.graph_version_ref

    resolved_run = Run(
        run_id=f"approval-webhook-resolved-{suffix}",
        thread_id=f"approval-webhook-resolved-{suffix}",
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        tenant_id=campaign.tenant_id,
        workspace_id=bootstrap.deployment.workspace_id,
    )
    escalated_run = Run(
        run_id=f"approval-webhook-escalated-{suffix}",
        thread_id=f"approval-webhook-escalated-{suffix}",
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        tenant_id=campaign.tenant_id,
        workspace_id=bootstrap.deployment.workspace_id,
    )
    await bootstrap.run_repository.put(resolved_run)
    await bootstrap.run_repository.put(escalated_run)

    resolved = await approval_service.create_pending(
        run=resolved_run,
        node=_node(graph_version_ref),
        input_payload={"fixture": "resolved"},
    )
    reviewer = ActorIdentity(
        subject="evaluation-webhook-reviewer",
        auth_method=AuthMethod.API_KEY,
        tenant_id=campaign.tenant_id,
        workspace_id=bootstrap.deployment.workspace_id,
    )
    resolve_scope = {
        "tenant_id": campaign.tenant_id,
        "workspace_id": bootstrap.deployment.workspace_id,
        "deployment_ref": deployment_ref,
        "graph_version_ref": graph_version_ref,
    }
    await approval_service.resolve(
        resolved.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=reviewer,
        **resolve_scope,
    )
    await approval_service.resolve(
        resolved.approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=reviewer,
        **resolve_scope,
    )

    escalated = await approval_service.create_pending(
        run=escalated_run,
        node=_node(graph_version_ref, escalation_action="alert"),
        input_payload={"fixture": "escalated"},
    )
    escalated.sla_deadline = datetime.now(UTC) - timedelta(seconds=1)
    await approval_service.repository.write(escalated)
    await asyncio.gather(
        approval_service.escalate(escalated.approval_id, **resolve_scope),
        approval_service.escalate(escalated.approval_id, **resolve_scope),
    )

    deliveries = await _wait_for_deliveries(
        bootstrap,
        subscription.subscription_id,
        count=4,
    )
    identities = sorted(_event_identity(delivery.payload_json) for delivery in deliveries)
    expected = sorted(
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
    if identities != expected or len({item.event_id for item in deliveries}) != 4:
        raise RuntimeError("approval webhook event identity or uniqueness mismatch")

    verifications: dict[str, dict[str, object]] = {}
    for run in (resolved_run, escalated_run):
        report = await AuditContinuityVerifier(
            bootstrap.audit_repository,
            signer=bootstrap.signer,
        ).verify_run(
            run.run_id,
            tenant_id=campaign.tenant_id,
            workspace_id=bootstrap.deployment.workspace_id,
            workspace_scoped=bootstrap.deployment.workspace_id is not None,
            deployment_ref=deployment_ref,
        )
        if not report.verified or report.signature_verified is not True:
            raise RuntimeError(f"signed audit verification failed for {run.run_id}")
        verifications[run.run_id] = {
            "verified": report.verified,
            "signature_verified": report.signature_verified,
            "record_count": report.record_count,
            "unsigned_record_count": report.unsigned_record_count,
        }

    return {
        "campaign_id": campaign.campaign_id,
        "tenant_id": campaign.tenant_id,
        "deployment_ref": deployment_ref,
        "graph_version_ref": graph_version_ref,
        "subscription_id": subscription.subscription_id,
        "provider_calls": 0,
        "external_action_calls": 0,
        "delivery_transport": "campaign-local-evaluation-sink",
        "events": [
            {
                "event_type": event_type,
                "approval_id": approval_id,
                "run_id": run_id,
                "thread_id": thread_id,
            }
            for event_type, approval_id, run_id, thread_id in identities
        ],
        "audit_verification": verifications,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--deployment-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    campaign = CampaignConfig.model_validate_json(args.campaign_config.read_text(encoding="utf-8"))
    result = asyncio.run(execute(campaign=campaign, deployment_ref=args.deployment_ref))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "pass", "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
