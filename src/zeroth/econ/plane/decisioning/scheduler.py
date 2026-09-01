"""Single-service scheduler for due tenant-scoped economic decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import logging

from sqlalchemy import select

from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.decisioning.models import DecisionSchedule
from zeroth.econ.plane.decisioning.service import run_due_decision_schedules
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

logger = logging.getLogger(__name__)


def eligible_tenant_ids(now: datetime) -> list[str]:
    """Discover due ownership internally; caller input never chooses tenants."""
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


def run_due_decision_scans(*, now: datetime | None = None) -> int:
    """Run one database-claimed pass over every tenant with a due schedule."""
    current = now or datetime.now(UTC)
    completed = 0
    for tenant_id in eligible_tenant_ids(current):
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


async def run_scheduler_loop(
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    run_once: Callable[[], int] = run_due_decision_scans,
) -> None:
    """Run immediately, then retry on a bounded cadence until shutdown."""
    if interval_seconds <= 0:
        raise ValueError("scheduler interval must be positive")
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_once)
        except Exception:  # noqa: BLE001
            logger.exception("cloud decision scheduler pass failed")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


__all__ = ["eligible_tenant_ids", "run_due_decision_scans", "run_scheduler_loop"]
