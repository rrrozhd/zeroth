"""Single-service scheduler for due tenant-scoped economic decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
import logging
from threading import Lock
from time import perf_counter, time
from typing import Any, Literal, Protocol

from sqlalchemy import func, select

from zeroth.econ.plane.database import SessionLocal
from zeroth.econ.plane.decisioning.models import (
    DecisionSchedule,
    ProbabilisticDecisionSchedule,
)
from zeroth.econ.plane.decisioning.service import (
    run_due_decision_schedules,
    run_due_probabilistic_decision_schedules,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

logger = logging.getLogger(__name__)

SchedulerFamily = Literal["deterministic", "probabilistic"]
SchedulerResult = Literal["success", "failure"]
_FAMILIES: tuple[SchedulerFamily, ...] = ("deterministic", "probabilistic")
_RESULTS: tuple[SchedulerResult, ...] = ("success", "failure")


class SchedulerObserver(Protocol):
    """Closed event vocabulary; dynamic identifiers are intentionally absent."""

    def record_family_attempt(self, family: SchedulerFamily) -> None: ...

    def record_family_failure(self, family: SchedulerFamily) -> None: ...

    def record_decisions_completed(self, family: SchedulerFamily, count: int) -> None: ...

    def record_pass(
        self,
        result: SchedulerResult,
        duration_seconds: float,
        completed_at_unix: float,
    ) -> None: ...

    def set_due_backlog(self, family: SchedulerFamily, count: int) -> None: ...

    def set_active_owner(self, active: bool) -> None: ...

    def record_duplicate_owner_attempt(self) -> None: ...


class _NoOpSchedulerObserver:
    def record_family_attempt(self, family: SchedulerFamily) -> None:
        return None

    def record_family_failure(self, family: SchedulerFamily) -> None:
        return None

    def record_decisions_completed(self, family: SchedulerFamily, count: int) -> None:
        return None

    def record_pass(
        self,
        result: SchedulerResult,
        duration_seconds: float,
        completed_at_unix: float,
    ) -> None:
        return None

    def set_due_backlog(self, family: SchedulerFamily, count: int) -> None:
        return None

    def set_active_owner(self, active: bool) -> None:
        return None

    def record_duplicate_owner_attempt(self) -> None:
        return None


_NOOP = _NoOpSchedulerObserver()
_observer: SchedulerObserver = _NOOP
_observer_lock = Lock()


def get_scheduler_observer() -> SchedulerObserver:
    with _observer_lock:
        return _observer


def set_scheduler_observer(observer: SchedulerObserver | None) -> None:
    """Install one process-local observer; ``None`` restores disabled behavior."""
    global _observer
    with _observer_lock:
        _observer = observer or _NOOP


def scheduler_observer_enabled() -> bool:
    with _observer_lock:
        return _observer is not _NOOP


def observe(method: str, *args: object) -> None:
    """Invoke instrumentation without allowing it to affect scheduler behavior."""
    try:
        callback = getattr(get_scheduler_observer(), method)
        callback(*args)
    except Exception:  # noqa: BLE001
        # Exporter errors may contain payloads, so telemetry failures are not
        # republished into scheduler logs.
        return None


def _require_family(family: SchedulerFamily) -> SchedulerFamily:
    if family not in _FAMILIES:
        raise ValueError("unsupported scheduler family")
    return family


def _require_result(result: SchedulerResult) -> SchedulerResult:
    if result not in _RESULTS:
        raise ValueError("unsupported scheduler result")
    return result


class OtelSchedulerObserver:
    """OTEL adapter whose only attributes are closed family/result values."""

    def __init__(self, meter: Any) -> None:
        # OpenTelemetry is optional. This path runs only after explicit OTLP enablement.
        from opentelemetry.metrics import Observation

        self._observation_factory = Observation
        self._lock = Lock()
        self._backlog: dict[SchedulerFamily, int] = {family: 0 for family in _FAMILIES}
        self._last_success = 0.0
        self._consecutive_failures = 0
        self._active_owner = 0
        self._scans = meter.create_counter(
            "ecp_scheduler_scans_total", description="Scheduler passes attempted"
        )
        self._passes = meter.create_counter(
            "ecp_scheduler_passes_total", description="Scheduler passes by result"
        )
        self._family_attempts = meter.create_counter(
            "ecp_scheduler_family_attempts_total", description="Family scans attempted"
        )
        self._family_failures = meter.create_counter(
            "ecp_scheduler_family_failures_total", description="Family scan failures"
        )
        self._decisions_completed = meter.create_counter(
            "ecp_scheduler_decisions_completed_total", description="Decisions completed"
        )
        self._pass_duration = meter.create_histogram(
            "ecp_scheduler_pass_duration_seconds",
            unit="s",
            description="Scheduler pass duration",
        )
        meter.create_observable_gauge(
            "ecp_scheduler_due_backlog",
            callbacks=[self._observe_backlog],
            description="Due schedule rows by family",
        )
        meter.create_observable_gauge(
            "ecp_scheduler_last_success_unixtime",
            callbacks=[self._observe_last_success],
            unit="s",
            description="Unix timestamp of the last fully successful pass",
        )
        meter.create_observable_gauge(
            "ecp_scheduler_consecutive_failures",
            callbacks=[self._observe_consecutive_failures],
            description="Consecutive failed scheduler passes",
        )
        meter.create_observable_gauge(
            "ecp_scheduler_active_owner",
            callbacks=[self._observe_active_owner],
            description="Whether this process owns the scheduler",
        )
        self._duplicate_attempts = meter.create_counter(
            "ecp_scheduler_duplicate_owner_attempts_total",
            description="Rejected duplicate ownership attempts",
        )

    def record_family_attempt(self, family: SchedulerFamily) -> None:
        family = _require_family(family)
        self._family_attempts.add(1, attributes={"family": family})

    def record_family_failure(self, family: SchedulerFamily) -> None:
        family = _require_family(family)
        self._family_failures.add(1, attributes={"family": family})

    def record_decisions_completed(self, family: SchedulerFamily, count: int) -> None:
        family = _require_family(family)
        self._decisions_completed.add(max(0, int(count)), attributes={"family": family})

    def record_pass(
        self,
        result: SchedulerResult,
        duration_seconds: float,
        completed_at_unix: float,
    ) -> None:
        result = _require_result(result)
        attributes = {"result": result}
        self._scans.add(1)
        self._passes.add(1, attributes=attributes)
        self._pass_duration.record(max(0.0, duration_seconds), attributes=attributes)
        with self._lock:
            if result == "success":
                self._last_success = completed_at_unix
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

    def set_due_backlog(self, family: SchedulerFamily, count: int) -> None:
        family = _require_family(family)
        with self._lock:
            self._backlog[family] = max(0, int(count))

    def set_active_owner(self, active: bool) -> None:
        with self._lock:
            self._active_owner = int(bool(active))

    def record_duplicate_owner_attempt(self) -> None:
        self._duplicate_attempts.add(1)

    def _observe_backlog(self, _options: object) -> Iterable[Any]:
        with self._lock:
            snapshot = dict(self._backlog)
        return [
            self._observation_factory(snapshot[family], attributes={"family": family})
            for family in _FAMILIES
        ]

    def _observe_last_success(self, _options: object) -> Iterable[Any]:
        with self._lock:
            value = self._last_success
        return [self._observation_factory(value)]

    def _observe_consecutive_failures(self, _options: object) -> Iterable[Any]:
        with self._lock:
            value = self._consecutive_failures
        return [self._observation_factory(value)]

    def _observe_active_owner(self, _options: object) -> Iterable[Any]:
        with self._lock:
            value = self._active_owner
        return [self._observation_factory(value)]


class _ScanCount(int):
    """Integer-compatible count carrying private full-pass health to the loop."""

    family_failed: bool

    def __new__(cls, value: int, *, family_failed: bool = False) -> _ScanCount:
        instance = int.__new__(cls, value)
        instance.family_failed = family_failed
        return instance


def eligible_tenant_ids(now: datetime) -> list[str]:
    """Discover due ownership internally; caller input never chooses tenants."""
    with SessionLocal() as db:
        deterministic = set(
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
        probabilistic = set(
            db.scalars(
                select(ProbabilisticDecisionSchedule.tenant_id)
                .where(
                    ProbabilisticDecisionSchedule.active.is_(True),
                    ProbabilisticDecisionSchedule.next_run_at <= now,
                )
                .distinct()
            )
        )
        return sorted(deterministic | probabilistic)


def due_schedule_backlog(now: datetime) -> dict[str, int]:
    """Count globally due rows without introducing tenant identifiers into telemetry."""
    with SessionLocal() as db:
        return {
            "deterministic": int(
                db.scalar(
                    select(func.count())
                    .select_from(DecisionSchedule)
                    .where(DecisionSchedule.active.is_(True), DecisionSchedule.next_run_at <= now)
                )
                or 0
            ),
            "probabilistic": int(
                db.scalar(
                    select(func.count())
                    .select_from(ProbabilisticDecisionSchedule)
                    .where(
                        ProbabilisticDecisionSchedule.active.is_(True),
                        ProbabilisticDecisionSchedule.next_run_at <= now,
                    )
                )
                or 0
            ),
        }


def run_due_decision_scans(*, now: datetime | None = None) -> int:
    """Run one database-claimed pass over every tenant with a due schedule."""
    current = now or datetime.now(UTC)
    completed = 0
    family_failed = False
    if scheduler_observer_enabled():
        try:
            backlog = due_schedule_backlog(current)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cloud decision scheduler backlog sample failed exception_type=%s",
                type(exc).__name__,
            )
        else:
            for family, count in backlog.items():
                observe("set_due_backlog", family, count)
    for tenant_id in eligible_tenant_ids(current):
        for family, run_family in (
            ("deterministic", run_due_decision_schedules),
            ("probabilistic", run_due_probabilistic_decision_schedules),
        ):
            observe("record_family_attempt", family)
            try:
                scope = (
                    TenantWideScopeContext.for_default_compatibility()
                    if tenant_id == "default"
                    else TenantWideScopeContext(tenant_id=tenant_id)
                )
                # Each family gets a clean transaction/session even when a
                # previous family's query, claim, refund or persistence fails.
                with SessionLocal() as db:
                    scoped = ScopedSession(db, scope)
                    family_completed = len(run_family(scoped, now=current))
                    completed += family_completed
                    observe("record_decisions_completed", family, family_completed)
            except Exception as exc:  # noqa: BLE001
                family_failed = True
                observe("record_family_failure", family)
                logger.error(
                    "cloud decision scheduler family failed family=%s exception_type=%s",
                    family, type(exc).__name__,
                )
    return _ScanCount(completed, family_failed=family_failed)


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
        started_at = perf_counter()
        result = "success"
        try:
            scan_count = await asyncio.to_thread(run_once)
            if getattr(scan_count, "family_failed", False):
                result = "failure"
        except Exception as exc:  # noqa: BLE001
            result = "failure"
            logger.error("cloud decision scheduler pass failed exception_type=%s", type(exc).__name__)
        finally:
            observe("record_pass", result, perf_counter() - started_at, time())
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


__all__ = [
    "OtelSchedulerObserver",
    "SchedulerObserver",
    "due_schedule_backlog",
    "eligible_tenant_ids",
    "get_scheduler_observer",
    "observe",
    "run_due_decision_scans",
    "run_scheduler_loop",
    "scheduler_observer_enabled",
    "set_scheduler_observer",
]
