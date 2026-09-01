"""Worker entry point for recurring economic decision scans."""

from __future__ import annotations

import dramatiq

from zeroth.econ.plane.common.worker import redis_broker
from zeroth.econ.plane.decisioning.scheduler import (
    eligible_tenant_ids as _eligible_tenant_ids,
)
from zeroth.econ.plane.decisioning.scheduler import (
    run_due_decision_scans as _run_due_decision_scans,
)

dramatiq.set_broker(redis_broker)

@dramatiq.actor(max_retries=0)
def process_due_decision_schedules() -> int:
    """Evaluate all currently due schedules; invoke this actor periodically."""

    return _run_due_decision_scans()
