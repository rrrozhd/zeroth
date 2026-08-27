"""Background task that polls for overdue approvals and escalates them.

Modeled after the RunWorker pattern: runs as an asyncio task started in
the application lifespan, loops forever with a configurable poll interval,
and handles its own errors gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from zeroth.governance.approvals.models import ApprovalDecision, ApprovalStatus
from zeroth.governance.approvals.service import ApprovalService

logger = logging.getLogger(__name__)


@dataclass
class ApprovalSLAChecker:
    """Periodically checks for overdue approvals and escalates them.

    Attributes:
        approval_service: Used to list overdue approvals and escalate them.
        webhook_service: Optional webhook service for emitting escalation events.
        poll_interval: Seconds between poll ticks (default 10).
    """

    approval_service: ApprovalService
    webhook_service: object | None = None  # Optional WebhookService to avoid circular import
    poll_interval: float = 10.0

    async def poll_loop(self) -> None:
        """Continuously check for and escalate overdue approvals until cancelled."""
        while True:
            try:
                overdue = await self.approval_service.repository.list_overdue()
                for record in overdue:
                    try:
                        escalated = await self.approval_service.escalate(
                            record.approval_id,
                            tenant_id=record.tenant_id,
                            workspace_id=record.workspace_id,
                            deployment_ref=record.deployment_ref,
                            graph_version_ref=record.graph_version_ref,
                        )
                        resolution = escalated.resolution
                        if (
                            escalated.status is ApprovalStatus.RESOLVED
                            and resolution is not None
                            and resolution.decision is ApprovalDecision.REJECT
                            and resolution.actor.subject == "sla_enforcer"
                        ):
                            await self.approval_service.schedule_continuation(
                                escalated.approval_id
                            )
                    except Exception:
                        logger.exception("failed to escalate approval %s", record.approval_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SLA checker poll error")
            await asyncio.sleep(self.poll_interval)
