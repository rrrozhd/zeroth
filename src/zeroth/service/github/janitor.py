"""Background maintenance for the GitHub integration surface.

Currently a single duty: pruning the webhook delivery dedup ledger after its
retention window. The worker follows the retention purge worker's shape (an
async poll loop started and cancelled by the service lifespan) and is kept
deliberately minimal so later phases (checkout-stage expiry, cache eviction)
can add sweeps without changing its wiring.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from zeroth.platform.primitives import utc_now
from zeroth.service.github.repository import SQLiteGitHubRepository

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 3600.0
_DEFAULT_DELIVERY_RETENTION = timedelta(days=7)


class GitHubMaintenanceWorker:
    """Periodic sweeper over the GitHub integration's persisted state."""

    def __init__(
        self,
        repository: SQLiteGitHubRepository,
        *,
        tenant_id: str = "default",
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        delivery_retention: timedelta = _DEFAULT_DELIVERY_RETENTION,
    ) -> None:
        self._repository = repository
        self._tenant_id = tenant_id
        self._poll_interval = poll_interval
        self._delivery_retention = delivery_retention

    @property
    def poll_interval(self) -> float:
        """Seconds between maintenance sweeps."""
        return self._poll_interval

    async def sweep_once(self) -> int:
        """Run one maintenance sweep; returns the number of pruned deliveries."""
        cutoff = utc_now() - self._delivery_retention
        pruned = await self._repository.prune_deliveries(self._tenant_id, cutoff)
        if pruned:
            logger.info("pruned %d expired github webhook delivery record(s)", pruned)
        return pruned

    async def poll_loop(self) -> None:
        """Sweep on the poll interval until cancelled by the service lifespan."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The sweep is best-effort hygiene; a failed pass must not end
                # the loop. Only the exception type could carry foreign text,
                # so the traceback is safe to log for the operator.
                logger.exception("github maintenance sweep failed")
            await asyncio.sleep(self._poll_interval)


__all__ = ["GitHubMaintenanceWorker"]
