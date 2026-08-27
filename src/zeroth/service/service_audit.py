"""Signed, metadata-only audit records for service control-plane transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from zeroth.governance.audit import ApprovalActionRecord, AuditRepository, NodeAuditRecord
from zeroth.governance.identity import ActorIdentity
from zeroth.platform.signing import NullSigner
from zeroth.platform.storage import AsyncConnection
from zeroth.platform.storage.scoped_table import BoundStructuredTable


def webhook_event_identity(payload_json: str) -> dict[str, str | None]:
    """Extract bounded correlation identifiers from a hidden webhook payload.

    Webhook bodies may contain arbitrary operator or workflow prose.  Delivery
    auditing therefore projects only the four durable identifiers needed to
    correlate the transition back to its historical run, thread, graph, and
    approval.
    """
    empty = {
        "run_id": None,
        "approval_id": None,
        "thread_id": None,
        "graph_version_ref": None,
    }
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return empty
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return empty

    def identifier(name: str) -> str | None:
        value = data.get(name)
        return value if isinstance(value, str) and 0 < len(value) <= 512 else None

    return {name: identifier(name) for name in empty}


@dataclass(frozen=True, slots=True)
class ServiceAuditRecorder:
    """Build signed service-event records without retaining request or payload prose."""

    repository: AuditRepository
    deployment: object
    require_signed: bool = False

    def _require_signing(self) -> None:
        signer = getattr(self.repository, "_signer", None)
        if self.require_signed and (signer is None or isinstance(signer, NullSigner)):
            raise RuntimeError("service control-plane transition requires audit signing")

    def ensure_signing_available(self) -> None:
        """Fail before a protected mutation when signed audit is unavailable."""
        self._require_signing()

    async def record_template_event(
        self,
        *,
        actor: ActorIdentity,
        template_name: str,
        template_version: int,
        transition: str,
        transaction: AsyncConnection | BoundStructuredTable | None = None,
    ) -> NodeAuditRecord:
        """Record a template mutation without retaining prompt or variable content."""
        self._require_signing()
        deployment_ref = getattr(self.deployment, "deployment_ref", "service")
        template_name_sha256 = hashlib.sha256(template_name.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        record = NodeAuditRecord(
            audit_id=f"template.{transition}:{uuid4().hex}",
            run_id=(f"service:{deployment_ref}:template:{template_name_sha256}:{template_version}"),
            node_id=f"template.{transition.removesuffix('d')}",
            graph_version_ref=getattr(self.deployment, "graph_version_ref", "service"),
            deployment_ref=deployment_ref,
            tenant_id=getattr(self.deployment, "tenant_id", "default"),
            workspace_id=getattr(self.deployment, "workspace_id", None),
            status="completed",
            actor=actor,
            execution_metadata={
                "template_name_sha256": template_name_sha256,
                "template_version": template_version,
                "template_transition": transition,
            },
            cost_usd=0.0,
            estimated_cost_usd=0.0,
            started_at=now,
            completed_at=now,
        )
        written = (
            await self.repository.write(record)
            if transaction is None
            else await self.repository.write_in_transaction(transaction, record)
        )
        if written.record_signature is None:
            raise RuntimeError("template control-plane audit was not signed")
        return written

    async def record_run_control_event(
        self,
        *,
        actor: ActorIdentity,
        run: Any,
        transition: str,
        descendant_count: int,
    ) -> NodeAuditRecord:
        """Record a metadata-only operator transition on one durable run."""
        self._require_signing()
        if not transition or not transition.replace("_", "").isalnum():
            raise ValueError("run control transition must be a short identifier")
        if descendant_count < 0:
            raise ValueError("descendant cancellation count cannot be negative")
        now = datetime.now(UTC)
        record = NodeAuditRecord(
            audit_id=f"run.control.{transition}:{uuid4().hex}",
            run_id=str(run.run_id),
            thread_id=getattr(run, "thread_id", None),
            node_id=f"run.control.{transition}",
            graph_version_ref=str(run.graph_version_ref),
            deployment_ref=str(run.deployment_ref),
            tenant_id=str(run.tenant_id),
            workspace_id=getattr(run, "workspace_id", None),
            status="completed",
            actor=actor,
            execution_metadata={
                "run_control_transition": transition,
                "descendant_cancellation_count": descendant_count,
            },
            cost_usd=0.0,
            estimated_cost_usd=0.0,
            started_at=now,
            completed_at=now,
        )
        written = await self.repository.write(record)
        if self.require_signed and written.record_signature is None:
            raise RuntimeError("run control-plane audit was not signed")
        return written

    async def record_webhook_event(
        self,
        *,
        node_id: str,
        actor: ActorIdentity | None,
        subscription_id: str,
        transition: str,
        delivery_id: str | None = None,
        event_id: str | None = None,
        dead_letter_id: str | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        approval_id: str | None = None,
        target_url: str | None = None,
        thread_id: str | None = None,
        graph_version_ref: str | None = None,
        attempt: int | None = None,
        upstream_status_code: int | None = None,
    ) -> NodeAuditRecord:
        """Record one webhook mutation/transition using typed IDs and URL digest only."""
        record = self.build_webhook_event(
            node_id=node_id,
            actor=actor,
            subscription_id=subscription_id,
            transition=transition,
            delivery_id=delivery_id,
            event_id=event_id,
            dead_letter_id=dead_letter_id,
            event_type=event_type,
            run_id=run_id,
            approval_id=approval_id,
            target_url=target_url,
            thread_id=thread_id,
            graph_version_ref=graph_version_ref,
            attempt=attempt,
            upstream_status_code=upstream_status_code,
        )
        written = await self.repository.write(record)
        self.validate_webhook_write(written)
        return written

    def build_webhook_event(
        self,
        *,
        node_id: str,
        actor: ActorIdentity | None,
        subscription_id: str,
        transition: str,
        delivery_id: str | None = None,
        event_id: str | None = None,
        dead_letter_id: str | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        approval_id: str | None = None,
        target_url: str | None = None,
        thread_id: str | None = None,
        graph_version_ref: str | None = None,
        attempt: int | None = None,
        upstream_status_code: int | None = None,
    ) -> NodeAuditRecord:
        """Build a metadata-only webhook record before any protected mutation."""
        self._require_signing()
        deployment_ref = getattr(self.deployment, "deployment_ref", "service")
        metadata: dict[str, Any] = {
            "webhook_subscription_id": subscription_id,
            "webhook_transition": transition,
        }
        if delivery_id is not None:
            metadata["webhook_delivery_id"] = delivery_id
        if event_id is not None:
            metadata["webhook_event_id"] = event_id
        if dead_letter_id is not None:
            metadata["webhook_dead_letter_id"] = dead_letter_id
        if event_type is not None:
            metadata["webhook_event_type"] = event_type
        if target_url is not None:
            metadata["target_url_sha256"] = hashlib.sha256(target_url.encode("utf-8")).hexdigest()
        if attempt is not None:
            metadata["attempt"] = attempt
        if upstream_status_code is not None:
            metadata["upstream_status_code"] = upstream_status_code

        now = datetime.now(UTC)
        record = NodeAuditRecord(
            audit_id=f"{node_id}:{uuid4().hex}",
            run_id=(run_id or f"service:{deployment_ref}:webhook:{subscription_id}"),
            thread_id=thread_id,
            node_id=node_id,
            graph_version_ref=(
                graph_version_ref or getattr(self.deployment, "graph_version_ref", "service")
            ),
            deployment_ref=deployment_ref,
            tenant_id=getattr(self.deployment, "tenant_id", "default"),
            workspace_id=getattr(self.deployment, "workspace_id", None),
            status="completed",
            actor=actor,
            execution_metadata=metadata,
            approval_actions=(
                [
                    ApprovalActionRecord(
                        approval_id=approval_id,
                        action=transition,
                        actor=actor,
                        occurred_at=now,
                    )
                ]
                if approval_id is not None
                else []
            ),
            cost_usd=0.0,
            estimated_cost_usd=0.0,
            started_at=now,
            completed_at=now,
        )
        return record

    def validate_webhook_write(self, written: NodeAuditRecord) -> None:
        """Reject an unsigned result while the caller's transaction can roll back."""
        if self.require_signed and written.record_signature is None:
            raise RuntimeError("service control-plane transition audit was not signed")
