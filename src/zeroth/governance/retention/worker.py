"""Background retention purge worker (WS-E).

Mirrors :class:`zeroth.governance.approvals.sla_checker.ApprovalSLAChecker` exactly: an
``@dataclass`` with an ``async poll_loop`` that runs until cancelled, catches and
logs per-iteration errors, and sleeps ``poll_interval`` seconds between sweeps.
Started in the service lifespan via ``asyncio.create_task`` and cancelled on
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from zeroth.governance.retention.policy_repository import RetentionPolicyRepository

if TYPE_CHECKING:
    # Annotation-only: importing the service here would put it on this package's
    # own import path, and its collaborators import back into this package.
    from zeroth.governance.retention.erasure_service import RetentionErasureService

logger = logging.getLogger(__name__)


@dataclass
class RetentionPurgeWorker:
    """Periodically purges aged, non-held runs per each tenant's TTL policy.

    Attributes:
        erasure_service: Performs the actual per-run erasure.
        policy_repository: Enumerates enabled tenant policies to sweep.
        poll_interval: Seconds between purge sweeps (default 3600).
    """

    tenant_id: str
    policy_repository: RetentionPolicyRepository
    erasure_service: RetentionErasureService
    poll_interval: float = 3600.0

    async def sweep_once(self) -> None:
        """Sweep the deployment owner's resolved policy once."""
        policy = await self.policy_repository.resolve()
        if policy.tenant_id != self.tenant_id:
            raise ValueError("retention policy does not match bound tenant")
        if not policy.enabled:
            return
        for sweep in (
            self.erasure_service.purge_runs,
            self.erasure_service.purge_audits,
        ):
            try:
                await sweep(self.tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "retention %s failed for tenant %s",
                    sweep.__name__,
                    self.tenant_id,
                )

    async def poll_loop(self) -> None:
        """Continuously sweep each tenant's TTL surfaces until cancelled.

        Run erasure (``purge_runs``) and audit tombstoning (``purge_audits``)
        are independent sweeps: a failure in one is logged and does not abort
        the other surface, the next tenant, or the next tick.
        """
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention purge worker poll error")
            await asyncio.sleep(self.poll_interval)
