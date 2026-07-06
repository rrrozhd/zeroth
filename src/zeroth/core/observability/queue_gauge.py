"""Background task that periodically records queue depth as a gauge metric."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QueueDepthGauge:
    """Polls pending run count and updates the metrics collector gauge."""

    run_repository: object  # RunRepository
    deployment_ref: str
    metrics_collector: object  # MetricsCollector
    poll_interval: float = 10.0

    async def run(self) -> None:
        """Poll forever; cancelled by the app lifespan on shutdown."""
        while True:
            try:
                count = await self.run_repository.count_pending(self.deployment_ref)
                self.metrics_collector.gauge_set("zeroth_queue_depth", float(count))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("queue gauge poll error")
            await asyncio.sleep(self.poll_interval)
