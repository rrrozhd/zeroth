"""Vendor-neutral hosted plan and usage enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession


@dataclass(frozen=True)
class PlanLimits:
    event_limit: int
    decision_scan_limit: int
    backtest_call_limit: int
    schedule_limit: int
    minimum_schedule_interval_minutes: int


PLAN_CATALOG: dict[str, PlanLimits] = {
    "trial": PlanLimits(
        event_limit=30_000,
        decision_scan_limit=1,
        backtest_call_limit=200,
        schedule_limit=1,
        minimum_schedule_interval_minutes=1440,
    ),
    "solo": PlanLimits(
        event_limit=100_000,
        decision_scan_limit=31,
        backtest_call_limit=4_000,
        schedule_limit=5,
        minimum_schedule_interval_minutes=1440,
    ),
    "team": PlanLimits(
        event_limit=1_000_000,
        decision_scan_limit=744,
        backtest_call_limit=20_000,
        schedule_limit=25,
        minimum_schedule_interval_minutes=60,
    ),
    "scale": PlanLimits(
        event_limit=10_000_000,
        decision_scan_limit=10_000,
        backtest_call_limit=100_000,
        schedule_limit=500,
        minimum_schedule_interval_minutes=15,
    ),
}


class EntitlementError(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _meter_label(meter: str) -> str:
    return {"events": "event", "decision_scans": "decision scan", "backtest_calls": "backtest call"}.get(
        meter, meter.replace("_", " ")
    )


def _active_subscription(db: ScopedSession) -> tuple[CloudSubscription, PlanLimits]:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("cloud entitlements require a tenant-scoped session")
    subscription = db.get(CloudSubscription, db.scope.tenant_id)
    now = datetime.now(UTC)
    if subscription is None or subscription.status not in {"active", "trialing"}:
        raise EntitlementError("active Zeroth Cloud subscription required")
    period_end = subscription.period_end
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    if period_end <= now:
        raise EntitlementError("Zeroth Cloud subscription period has ended")
    limits = PLAN_CATALOG.get(subscription.plan)
    if limits is None:
        raise EntitlementError("Zeroth Cloud plan is not recognized")
    return subscription, limits


def reserve_usage(db: ScopedSession, meter: str, amount: int = 1) -> bool:
    """Atomically reserve metered capacity; return False when enforcement is off."""

    if not settings.cloud_entitlements_enabled:
        return False
    subscription, limits = _active_subscription(db)
    limit = {
        "events": limits.event_limit,
        "decision_scans": limits.decision_scan_limit,
        "backtest_calls": limits.backtest_call_limit,
    }.get(meter)
    if limit is None:
        raise ValueError(f"unknown cloud usage meter: {meter}")
    if amount < 1:
        raise ValueError("usage reservation amount must be positive")
    identity = (subscription.tenant_id, subscription.period_start, meter)
    existing = db.get(CloudUsageCounter, identity)
    now = datetime.now(UTC)
    if existing is None:
        if amount > limit:
            raise EntitlementError(f"{subscription.plan} {_meter_label(meter)} limit reached")
        db.add(
            CloudUsageCounter(
                tenant_id=subscription.tenant_id,
                period_start=subscription.period_start,
                meter=meter,
                quantity=amount,
                updated_at=now,
            )
        )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()

    claimed = db.execute(
        update(CloudUsageCounter)
        .where(
            CloudUsageCounter.tenant_id == subscription.tenant_id,
            CloudUsageCounter.period_start == subscription.period_start,
            CloudUsageCounter.meter == meter,
            CloudUsageCounter.quantity <= limit - amount,
        )
        .values(
            quantity=CloudUsageCounter.quantity + amount,
            updated_at=now,
        )
        .returning(CloudUsageCounter.quantity)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    db.commit()
    if claimed is None:
        raise EntitlementError(f"{subscription.plan} {_meter_label(meter)} limit reached")
    return True


def release_usage(db: ScopedSession, meter: str, amount: int = 1) -> None:
    if not settings.cloud_entitlements_enabled:
        return
    subscription, _limits = _active_subscription(db)
    db.execute(
        update(CloudUsageCounter)
        .where(
            CloudUsageCounter.tenant_id == subscription.tenant_id,
            CloudUsageCounter.period_start == subscription.period_start,
            CloudUsageCounter.meter == meter,
        )
        .values(
            quantity=case(
                (CloudUsageCounter.quantity >= amount, CloudUsageCounter.quantity - amount),
                else_=0,
            ),
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()


def plan_limits(db: ScopedSession) -> PlanLimits | None:
    if not settings.cloud_entitlements_enabled:
        return None
    return _active_subscription(db)[1]


def assert_schedule_allowed(db: ScopedSession, interval_minutes: int) -> None:
    if not settings.cloud_entitlements_enabled:
        return
    subscription, limits = _active_subscription(db)
    if interval_minutes < limits.minimum_schedule_interval_minutes:
        raise EntitlementError(
            f"{subscription.plan} schedules must be at least "
            f"{limits.minimum_schedule_interval_minutes} minutes apart"
        )
    from zeroth.econ.plane.decisioning.models import DecisionSchedule

    active = len(
        list(
            db.scalars(
                select(DecisionSchedule).where(DecisionSchedule.active.is_(True))
            )
        )
    )
    if active >= limits.schedule_limit:
        raise EntitlementError(f"{subscription.plan} decision schedule limit reached")
