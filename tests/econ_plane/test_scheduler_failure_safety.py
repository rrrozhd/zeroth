"""Fault-injection checks; actual scoping/persistence and HTTP readiness."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane import config, main
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning import scheduler, service
from zeroth.econ.plane.decisioning.models import DecisionSchedule, ProbabilisticDecisionSchedule
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    ProbabilisticDecisionScheduleCreate,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.schema_revision import SchemaRevision
from zeroth.platform.storage.scoping import TenantWideScopeContext


@pytest.mark.parametrize(
    "enabled,done,schema,expected",
    [
        (False, None, "current", 200),
        (True, None, "current", 503),
        (True, True, "current", 503),
        (True, False, "current", 200),
        (False, None, "behind", 503),
        (False, None, "unknown", 503),
    ],
)
def test_readiness_status_reflects_required_scheduler(monkeypatch, enabled, done, schema, expected):
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", enabled)
    task = None if done is None else SimpleNamespace(done=lambda: done)
    monkeypatch.setattr(main.app.state, "cloud_scheduler_task", task, raising=False)
    monkeypatch.setattr(
        main.common_bootstrap,
        "schema_revision",
        lambda: SchemaRevision(applied="head", head="head", state=schema),
    )
    # No lifespan: intentionally exercise the missing-scheduler readiness state.
    client = TestClient(main.app)
    response = client.get("/health/ready")
    assert response.status_code == expected, response.text
    assert client.get("/health").status_code == expected


@pytest.mark.parametrize("failing_family", ["deterministic", "probabilistic"])
def test_one_tenant_failure_does_not_skip_other_tenants(
    tmp_path, monkeypatch, caplog, failing_family
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'isolation.db'}")
    seen = []
    monkeypatch.setattr(scheduler, "eligible_tenant_ids", lambda now: ["tenant-a", "tenant-b"])
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: Session(engine))

    def deterministic(db, *, now):
        seen.append((db.scope.tenant_id, "deterministic"))
        if db.scope.tenant_id == "tenant-a" and failing_family == "deterministic":
            raise RuntimeError("SELECT token='private-audit-marker'")
        return [object()]

    def probabilistic(db, *, now):
        seen.append((db.scope.tenant_id, "probabilistic"))
        if db.scope.tenant_id == "tenant-a" and failing_family == "probabilistic":
            raise RuntimeError("SELECT token='private-audit-marker'")
        return [object()]

    monkeypatch.setattr(scheduler, "run_due_decision_schedules", deterministic)
    monkeypatch.setattr(scheduler, "run_due_probabilistic_decision_schedules", probabilistic)
    try:
        scheduler.run_due_decision_scans(now=datetime.now(UTC))
    finally:
        engine.dispose()
    assert ("tenant-b", "deterministic") in seen
    assert ("tenant-b", "probabilistic") in seen
    assert ("tenant-a", "probabilistic") in seen
    assert "private-audit-marker" not in caplog.text
    assert "SELECT" not in caplog.text


@pytest.mark.parametrize("kind", ["probabilistic", "deterministic"])
def test_schedule_retained_error_does_not_republish_sql_parameters(
    tmp_path, monkeypatch, caplog, kind
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'errors.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", False)
    now = datetime.now(UTC)

    def failure(*args, **kwargs):
        raise RuntimeError("SELECT sensitive_payload WHERE token='private-audit-marker'")

    monkeypatch.setattr(service, "evaluate_probabilistic_migration_from_store", failure)
    monkeypatch.setattr(service, "compare_versions_from_store", failure)
    try:
        with Session(engine) as raw:
            db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
            if kind == "probabilistic":
                service.create_probabilistic_decision_schedule(
                    db,
                    ProbabilisticDecisionScheduleCreate.model_validate(
                        {
                            "evidence_source": {
                                "workload": "w",
                                "incumbent_model": "a",
                                "candidate_model": "b",
                            },
                            "policy": {},
                            "simulations": 100,
                        }
                    ),
                    created_by="test-owner",
                    now=now,
                )
                assert service.run_due_probabilistic_decision_schedules(db, now=now) == []
                schedule = service.list_probabilistic_decision_schedules(db)[0]
            else:
                service.create_decision_schedule(
                    db,
                    DecisionScheduleCreate(
                        workflow="w", baseline_version="a", candidate_version="b"
                    ),
                    created_by="test-owner",
                )
                assert service.run_due_decision_schedules(db, now=now + timedelta(seconds=1)) == []
                schedule = service.list_decision_schedules(db)[0]
            assert schedule.last_error, "failure must remain visible as a safe error code"
            assert "private-audit-marker" not in schedule.last_error
            assert "SELECT" not in schedule.last_error
            assert "private-audit-marker" not in caplog.text
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_pass_failure_is_sanitized_and_retried(caplog):
    stop = asyncio.Event()
    calls = 0

    def run_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("SELECT token='private-audit-marker'")
        stop.set()
        return 0

    await scheduler.run_scheduler_loop(stop, interval_seconds=0.001, run_once=run_once)
    assert calls == 2
    assert "private-audit-marker" not in caplog.text
    assert "SELECT" not in caplog.text


@pytest.mark.parametrize("kind", ["probabilistic", "deterministic"])
@pytest.mark.parametrize(
    "legacy_error", [None, "schedule_execution_failed", "SELECT token='private-legacy-marker'"]
)
def test_legacy_schedule_error_is_sanitized_on_read_without_erasure(
    tmp_path, monkeypatch, kind, legacy_error
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy-errors.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", False)
    try:
        with Session(engine) as raw:
            db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
            if kind == "probabilistic":
                created = service.create_probabilistic_decision_schedule(
                    db,
                    ProbabilisticDecisionScheduleCreate.model_validate(
                        {
                            "evidence_source": {
                                "workload": "w",
                                "incumbent_model": "a",
                                "candidate_model": "b",
                            },
                            "policy": {},
                            "simulations": 100,
                        }
                    ),
                    created_by="test-owner",
                    now=datetime.now(UTC),
                )
                model = ProbabilisticDecisionSchedule
                list_schedules = service.list_probabilistic_decision_schedules
            else:
                created = service.create_decision_schedule(
                    db,
                    DecisionScheduleCreate(
                        workflow="w", baseline_version="a", candidate_version="b"
                    ),
                    created_by="test-owner",
                )
                model = DecisionSchedule
                list_schedules = service.list_decision_schedules
            row = db.get(model, created.schedule_id)
            row.last_error = legacy_error
            db.commit()
            exposed = list_schedules(db)[0]
            assert exposed.last_error == (
                None if legacy_error is None else "schedule_execution_failed"
            )
            db.refresh(row)
            assert row.last_error == legacy_error
    finally:
        engine.dispose()
