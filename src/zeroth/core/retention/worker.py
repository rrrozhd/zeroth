"""Background retention purge worker (WS-E).

Mirrors :class:`zeroth.core.approvals.sla_checker.ApprovalSLAChecker` exactly: an
``@dataclass`` with an ``async poll_loop`` that runs until cancelled, catches and
logs per-iteration errors, and sleeps ``poll_interval`` seconds between sweeps.
Started in the service lifespan via ``asyncio.create_task`` and cancelled on
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from zeroth.core.retention.erasure_service import RetentionErasureService
from zeroth.core.retention.policy_repository import RetentionPolicyRepository

logger = logging.getLogger(__name__)


@dataclass
class RetentionPurgeWorker:
    """Periodically purges aged, non-held runs per each tenant's TTL policy.

    Attributes:
        erasure_service: Performs the actual per-run erasure.
        policy_repository: Enumerates enabled tenant policies to sweep.
        poll_interval: Seconds between purge sweeps (default 3600).
    """

    erasure_service: RetentionErasureService
    policy_repository: RetentionPolicyRepository
    poll_interval: float = 3600.0

    async def poll_loop(self) -> None:
        """Continuously purge each tenant's aged runs until cancelled.

        A failure purging one tenant is logged and does not abort the sweep or
        the loop — the next tenant, and the next tick, still run.
        """
        while True:
            try:
                for policy in await self.policy_repository.list_all_enabled():
                    try:
                        await self.erasure_service.purge_tenant(policy.tenant_id)
                    except Exception:
                        logger.exception("retention purge failed for tenant %s", policy.tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention purge worker poll error")
            await asyncio.sleep(self.poll_interval)
