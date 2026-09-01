"""Worker entry point for recurring economic decision scans."""

from __future__ import annotations

from datetime import UTC, datetime

import dramatiq
from sqlalchemy import select

from zeroth.econ.plane.common.worker import redis_broker
from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.decisioning.models import DecisionSchedule
from zeroth.econ.plane.decisioning.service import run_due_decision_schedules
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

dramatiq.set_broker(redis_broker)


def _eligible_tenant_ids(now: datetime) -> list[str]:
    """Discover due tenants internally; actor input never selects ownership."""

    with SessionLocal() as db:
        return list(
            db.scalars(
                select(DecisionSchedule.tenant_id)
                .where(
                    DecisionSchedule.active.is_(True),
                    DecisionSchedule.next_run_at <= now,
                )
                .distinct()
                .order_by(DecisionSchedule.tenant_id)
            )
        )


def _run_due_decision_scans(*, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    completed = 0
    for tenant_id in _eligible_tenant_ids(current):
        scope = (
            TenantWideScopeContext.for_default_compatibility()
            if tenant_id == "default"
            else TenantWideScopeContext(tenant_id=tenant_id)
        )
        with SessionLocal() as db:
            completed += len(
                run_due_decision_schedules(ScopedSession(db, scope), now=current)
            )
    return completed


@dramatiq.actor(max_retries=0)
def process_due_decision_schedules() -> int:
    """Evaluate all currently due schedules; invoke this actor periodically."""

    return _run_due_decision_scans()
