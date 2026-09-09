from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
import subprocess
import sys
import textwrap
from types import SimpleNamespace

from fastapi import FastAPI
import pytest


class RecordingObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, object]] = []
        self.fail = fail

    def _record(self, name: str, value: object) -> None:
        if self.fail:
            raise RuntimeError("SELECT secret='instrumentation-marker'")
        self.events.append((name, value))

    def record_family_attempt(self, family: str) -> None:
        self._record("attempt", family)

    def record_family_failure(self, family: str) -> None:
        self._record("family_failure", family)

    def record_decisions_completed(self, family: str, count: int) -> None:
        self._record("completed", (family, count))

    def record_pass(self, result: str, duration_seconds: float, completed_at_unix: float) -> None:
        self._record("pass", (result, duration_seconds, completed_at_unix))

    def set_due_backlog(self, family: str, count: int) -> None:
        self._record("backlog", (family, count))

    def set_active_owner(self, active: bool) -> None:
        self._record("owner", active)

    def record_duplicate_owner_attempt(self) -> None:
        self._record("duplicate", 1)


@pytest.fixture(autouse=True)
def reset_scheduler_observer():
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    set_scheduler_observer(None)
    yield
    set_scheduler_observer(None)


def test_family_failure_metrics_do_not_skip_other_family_or_later_tenant(monkeypatch) -> None:
    from zeroth.econ.plane.decisioning import scheduler
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    observer = RecordingObserver()
    set_scheduler_observer(observer)
    seen: list[tuple[str, str]] = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    monkeypatch.setattr(scheduler, "ScopedSession", lambda db, scope: SimpleNamespace(scope=scope))
    monkeypatch.setattr(scheduler, "eligible_tenant_ids", lambda now: ["tenant-a", "tenant-b"])
    monkeypatch.setattr(
        scheduler,
        "due_schedule_backlog",
        lambda now: {"deterministic": 4, "probabilistic": 2},
    )

    def deterministic(db, *, now):
        seen.append((db.scope.tenant_id, "deterministic"))
        if db.scope.tenant_id == "tenant-a":
            raise RuntimeError("private-family-marker")
        return [object(), object()]

    def probabilistic(db, *, now):
        seen.append((db.scope.tenant_id, "probabilistic"))
        return [object()]

    monkeypatch.setattr(scheduler, "run_due_decision_schedules", deterministic)
    monkeypatch.setattr(scheduler, "run_due_probabilistic_decision_schedules", probabilistic)

    assert scheduler.run_due_decision_scans(now=datetime.now(UTC)) == 4
    assert seen == [
        ("tenant-a", "deterministic"),
        ("tenant-a", "probabilistic"),
        ("tenant-b", "deterministic"),
        ("tenant-b", "probabilistic"),
    ]
    assert ("family_failure", "deterministic") in observer.events
    assert observer.events.count(("attempt", "deterministic")) == 2
    assert observer.events.count(("attempt", "probabilistic")) == 2
    assert ("backlog", ("deterministic", 4)) in observer.events
    assert ("backlog", ("probabilistic", 2)) in observer.events
    assert ("completed", ("deterministic", 2)) in observer.events
    assert observer.events.count(("completed", ("probabilistic", 1))) == 2


@pytest.mark.asyncio
async def test_pass_failure_then_success_records_recovery(monkeypatch) -> None:
    from zeroth.econ.plane.decisioning import scheduler
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    observer = RecordingObserver()
    set_scheduler_observer(observer)
    stop = asyncio.Event()
    calls = 0
    ticks = iter([10.0, 10.25, 20.0, 20.5])
    monkeypatch.setattr(scheduler, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(scheduler, "time", lambda: 1234.0)

    def run_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private-pass-marker")
        stop.set()
        return 0

    await scheduler.run_scheduler_loop(stop, interval_seconds=0.001, run_once=run_once)

    passes = [value for name, value in observer.events if name == "pass"]
    assert passes == [("failure", 0.25, 1234.0), ("success", 0.5, 1234.0)]


@pytest.mark.asyncio
async def test_actual_scan_family_failure_is_not_reported_as_full_pass_success(
    monkeypatch,
) -> None:
    from zeroth.econ.plane.decisioning import scheduler
    from zeroth.econ.plane.decisioning.scheduler import (
        OtelSchedulerObserver,
        set_scheduler_observer,
    )

    meter = Meter()
    observer = OtelSchedulerObserver(meter)
    set_scheduler_observer(observer)
    stop = asyncio.Event()
    seen: list[tuple[str, str]] = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    monkeypatch.setattr(scheduler, "ScopedSession", lambda db, scope: SimpleNamespace(scope=scope))
    monkeypatch.setattr(scheduler, "eligible_tenant_ids", lambda now: ["tenant-a", "tenant-b"])
    monkeypatch.setattr(
        scheduler,
        "due_schedule_backlog",
        lambda now: {"deterministic": 1, "probabilistic": 2},
    )

    def deterministic(db, *, now):
        seen.append((db.scope.tenant_id, "deterministic"))
        if db.scope.tenant_id == "tenant-a":
            raise RuntimeError("private-family-marker")
        return [object()]

    def probabilistic(db, *, now):
        seen.append((db.scope.tenant_id, "probabilistic"))
        return [object()]

    monkeypatch.setattr(scheduler, "run_due_decision_schedules", deterministic)
    monkeypatch.setattr(scheduler, "run_due_probabilistic_decision_schedules", probabilistic)

    def run_once() -> int:
        stop.set()
        return scheduler.run_due_decision_scans(now=datetime.now(UTC))

    await scheduler.run_scheduler_loop(stop, interval_seconds=1, run_once=run_once)

    assert seen == [
        ("tenant-a", "deterministic"),
        ("tenant-a", "probabilistic"),
        ("tenant-b", "deterministic"),
        ("tenant-b", "probabilistic"),
    ]
    assert meter.instruments["ecp_scheduler_passes_total"].points == [(1, {"result": "failure"})]
    assert [
        point.value for point in meter.callbacks["ecp_scheduler_last_success_unixtime"](None)
    ] == [0.0]
    assert [
        point.value for point in meter.callbacks["ecp_scheduler_consecutive_failures"](None)
    ] == [1]


@pytest.mark.asyncio
async def test_lifecycle_reports_duplicate_and_clears_owner_after_cancelled_stop() -> None:
    from zeroth.econ.plane.decisioning.lifecycle import start_scheduler, stop_scheduler
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    observer = RecordingObserver()
    set_scheduler_observer(observer)
    app = FastAPI()
    started = asyncio.Event()
    release = asyncio.Event()

    async def loop(stop, *, interval_seconds):
        started.set()
        await release.wait()

    handle = start_scheduler(app, owner=object(), enabled=True, interval_seconds=1, run_loop=loop)
    assert handle is not None
    await started.wait()
    with pytest.raises(RuntimeError, match="owner"):
        start_scheduler(app, owner=object(), enabled=True, interval_seconds=1, run_loop=loop)

    stopper = asyncio.create_task(stop_scheduler(app, handle))
    await asyncio.sleep(0)
    stopper.cancel()
    await asyncio.sleep(0)
    assert app.state.cloud_scheduler_handle is handle
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopper

    assert app.state.cloud_scheduler_handle is None
    assert observer.events == [("owner", True), ("duplicate", 1), ("owner", False)]


def test_instrumentation_failure_cannot_change_scan_result(monkeypatch, caplog) -> None:
    from zeroth.econ.plane.decisioning import scheduler
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    set_scheduler_observer(RecordingObserver(fail=True))

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(scheduler, "SessionLocal", Session)
    monkeypatch.setattr(scheduler, "ScopedSession", lambda db, scope: SimpleNamespace(scope=scope))
    monkeypatch.setattr(scheduler, "eligible_tenant_ids", lambda now: ["tenant-a"])
    monkeypatch.setattr(
        scheduler,
        "due_schedule_backlog",
        lambda now: {"deterministic": 1, "probabilistic": 1},
    )
    monkeypatch.setattr(scheduler, "run_due_decision_schedules", lambda db, now: [1])
    monkeypatch.setattr(scheduler, "run_due_probabilistic_decision_schedules", lambda db, now: [2])

    assert scheduler.run_due_decision_scans(now=datetime.now(UTC)) == 2
    assert "instrumentation-marker" not in caplog.text
    assert "SELECT" not in caplog.text


def test_disabled_observer_does_not_sample_backlog(monkeypatch) -> None:
    from zeroth.econ.plane.decisioning import scheduler

    monkeypatch.setattr(scheduler, "eligible_tenant_ids", lambda now: [])
    monkeypatch.setattr(
        scheduler,
        "due_schedule_backlog",
        lambda now: pytest.fail("disabled telemetry must not query backlog"),
    )

    assert scheduler.run_due_decision_scans(now=datetime.now(UTC)) == 0


def test_due_backlog_counts_rows_by_closed_family(tmp_path, monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zeroth.econ.plane.database import Base
    from zeroth.econ.plane.decisioning import scheduler
    from zeroth.econ.plane.decisioning.models import (
        DecisionSchedule,
        ProbabilisticDecisionSchedule,
    )

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backlog.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    common = {
        "tenant_id": "tenant-private",
        "policy_json": {},
        "interval_minutes": 60,
        "last_run_at": None,
        "last_decision_id": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "created_by": "actor-private",
    }
    with Session(engine) as db:
        db.add_all(
            [
                DecisionSchedule(
                    schedule_id="det-due",
                    workflow="workload-private",
                    baseline_version="a",
                    candidate_version="b",
                    outcome_type="success",
                    active=True,
                    next_run_at=now,
                    **common,
                ),
                DecisionSchedule(
                    schedule_id="det-inactive",
                    workflow="workload-private",
                    baseline_version="a",
                    candidate_version="b",
                    outcome_type="success",
                    active=False,
                    next_run_at=now,
                    **common,
                ),
                ProbabilisticDecisionSchedule(
                    schedule_id="prob-due",
                    evidence_source_json={},
                    simulations=100,
                    seed=7,
                    active=True,
                    next_run_at=now,
                    **common,
                ),
            ]
        )
        db.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: Session(engine))
    try:
        assert scheduler.due_schedule_backlog(now) == {
            "deterministic": 1,
            "probabilistic": 1,
        }
    finally:
        engine.dispose()


def test_global_scheduler_metrics_are_absent_from_tenant_prometheus_response(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zeroth.econ.plane.auth import models as _auth_models  # noqa: F401
    from zeroth.econ.plane.billing import models as _billing_models  # noqa: F401
    from zeroth.econ.plane.capabilities import models as _capability_models  # noqa: F401
    from zeroth.econ.plane.connectors.service import render_prometheus_metrics
    from zeroth.econ.plane.counterfactual import models as _counterfactual_models  # noqa: F401
    from zeroth.econ.plane.costing import models as _costing_models  # noqa: F401
    from zeroth.econ.plane.database import Base
    from zeroth.econ.plane.enforcement import models as _enforcement_models  # noqa: F401
    from zeroth.econ.plane.instrumentation import models as _instrumentation_models  # noqa: F401
    from zeroth.econ.plane.performance import models as _performance_models  # noqa: F401
    from zeroth.econ.plane.scoped_session import ScopedSession
    from zeroth.platform.storage.scoping import TenantWideScopeContext

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'tenant-metrics.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as raw:
            scoped = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-private"))
            rendered = render_prometheus_metrics(scoped)
        assert "ecp_scheduler_" not in rendered
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_failed_scheduler_task_clears_owner_when_lifecycle_drains() -> None:
    from zeroth.econ.plane.decisioning.lifecycle import start_scheduler, stop_scheduler
    from zeroth.econ.plane.decisioning.scheduler import set_scheduler_observer

    observer = RecordingObserver()
    set_scheduler_observer(observer)
    app = FastAPI()

    async def failing_loop(stop, *, interval_seconds):
        raise RuntimeError("startup-task-failure")

    handle = start_scheduler(
        app, owner=object(), enabled=True, interval_seconds=1, run_loop=failing_loop
    )
    assert handle is not None
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="startup-task-failure"):
        await stop_scheduler(app, handle)

    assert app.state.cloud_scheduler_handle is None
    assert observer.events == [("owner", True), ("owner", False)]


class Instrument:
    def __init__(self) -> None:
        self.points: list[tuple[int | float, dict[str, str]]] = []

    def add(self, value, *, attributes=None) -> None:
        self.points.append((value, attributes or {}))

    def record(self, value, *, attributes=None) -> None:
        self.points.append((value, attributes or {}))


class Meter:
    def __init__(self) -> None:
        self.instruments: dict[str, Instrument] = {}
        self.callbacks: dict[str, object] = {}

    def create_counter(self, name, **kwargs):
        return self.instruments.setdefault(name, Instrument())

    def create_histogram(self, name, **kwargs):
        return self.instruments.setdefault(name, Instrument())

    def create_observable_gauge(self, name, callbacks, **kwargs):
        self.callbacks[name] = callbacks[0]
        return self.instruments.setdefault(name, Instrument())


def test_otel_adapter_emits_complete_signal_set_with_closed_dimensions() -> None:
    from zeroth.econ.plane.decisioning.scheduler import OtelSchedulerObserver

    meter = Meter()
    observer = OtelSchedulerObserver(meter)
    observer.set_due_backlog("deterministic", 3)
    observer.set_due_backlog("probabilistic", 5)
    observer.record_family_attempt("deterministic")
    observer.record_family_failure("probabilistic")
    observer.record_decisions_completed("deterministic", 2)
    observer.record_pass("failure", 0.4, 100.0)
    observer.record_pass("failure", 0.6, 101.0)
    assert [
        (point.value, dict(point.attributes or {}))
        for point in meter.callbacks["ecp_scheduler_last_success_unixtime"](None)
    ] == [(0.0, {})]
    assert [
        (point.value, dict(point.attributes or {}))
        for point in meter.callbacks["ecp_scheduler_consecutive_failures"](None)
    ] == [(2, {})]
    observer.record_pass("success", 0.2, 102.0)
    observer.set_active_owner(True)
    observer.record_duplicate_owner_attempt()

    expected = {
        "ecp_scheduler_scans_total",
        "ecp_scheduler_passes_total",
        "ecp_scheduler_family_attempts_total",
        "ecp_scheduler_family_failures_total",
        "ecp_scheduler_decisions_completed_total",
        "ecp_scheduler_pass_duration_seconds",
        "ecp_scheduler_due_backlog",
        "ecp_scheduler_last_success_unixtime",
        "ecp_scheduler_consecutive_failures",
        "ecp_scheduler_active_owner",
        "ecp_scheduler_duplicate_owner_attempts_total",
    }
    assert set(meter.instruments) == expected

    gauge_points = []
    for callback in meter.callbacks.values():
        gauge_points.extend(callback(None))
    all_points = [point for instrument in meter.instruments.values() for point in instrument.points]
    all_points.extend((point.value, dict(point.attributes or {})) for point in gauge_points)
    assert all(set(attrs) <= {"family", "result"} for _, attrs in all_points)
    assert all(
        attrs.get("family") in {None, "deterministic", "probabilistic"}
        and attrs.get("result") in {None, "success", "failure"}
        for _, attrs in all_points
    )

    gauge_values = defaultdict(list)
    for name, callback in meter.callbacks.items():
        gauge_values[name] = [
            (point.value, dict(point.attributes or {})) for point in callback(None)
        ]
    assert gauge_values["ecp_scheduler_due_backlog"] == [
        (3, {"family": "deterministic"}),
        (5, {"family": "probabilistic"}),
    ]
    assert gauge_values["ecp_scheduler_last_success_unixtime"] == [(102.0, {})]
    assert gauge_values["ecp_scheduler_consecutive_failures"] == [(0, {})]
    assert gauge_values["ecp_scheduler_active_owner"] == [(1, {})]


def test_default_scheduler_and_lifecycle_work_without_opentelemetry_imports() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import sys
        from fastapi import FastAPI

        class BlockOpenTelemetry(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
                    raise ImportError("opentelemetry deliberately unavailable")
                return None

        sys.meta_path.insert(0, BlockOpenTelemetry())
        from zeroth.econ.plane.decisioning import scheduler
        from zeroth.econ.plane.decisioning.lifecycle import start_scheduler, stop_scheduler

        async def main():
            app = FastAPI()
            async def loop(stop, *, interval_seconds):
                await stop.wait()
            handle = start_scheduler(
                app, owner=object(), enabled=True, interval_seconds=1, run_loop=loop
            )
            await asyncio.sleep(0)
            await stop_scheduler(app, handle)
            assert scheduler.run_due_decision_scans
            print("default-path-ok")

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "default-path-ok"
