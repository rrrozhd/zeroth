"""Background task that polls for overdue approvals and escalates them.

Modeled after the RunWorker pattern: runs as an asyncio task started in
the application lifespan, loops forever with a configurable poll interval,
and handles its own errors gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from zeroth.approvals.service import ApprovalService

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
                        escalated = await self.approval_service.escalate(record.approval_id)
                        await self._emit_escalation_event(escalated)
                    except Exception:
                        logger.exception(
                            "failed to escalate approval %s", record.approval_id
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SLA checker poll error")
            await asyncio.sleep(self.poll_interval)

    async def _emit_escalation_event(self, record) -> None:
        """Emit an approval.escalated webhook event if a webhook service is available."""
        if self.webhook_service is None:
            return
        try:
            await self.webhook_service.emit_event(
                event_type="approval.escalated",
                deployment_ref=record.deployment_ref,
                tenant_id=record.tenant_id,
                data={
                    "approval_id": record.approval_id,
                    "run_id": record.run_id,
                    "node_id": record.node_id,
                    "escalation_action": record.escalation_action or "alert",
                    "sla_deadline": (
                        record.sla_deadline.isoformat() if record.sla_deadline else None
                    ),
                },
            )
        except Exception:
            logger.exception(
                "failed to emit escalation webhook for approval %s",
                record.approval_id,
            )
