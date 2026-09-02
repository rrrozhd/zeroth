from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.models import (
    ForecastCalibrationRecord,
    ProbabilisticMigrationDecisionRecord,
)
from zeroth.econ.plane.decisioning.schemas import ProbabilisticDecisionScheduleCreate
from zeroth.econ.plane.decisioning.service import (
    create_probabilistic_decision_schedule,
    run_due_probabilistic_decision_schedules,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext
from zeroth.econ.plane.decisioning.workers import _run_due_decision_scans


def _seed_pair(db: Session, *, index: int, when: datetime) -> None:
    for model, cost, kind in (
        ("model-a", "1.00", "production"),
        ("model-b", "0.50", "synthetic_control"),
    ):
        join_key = f"case-{index}:{model}"
        db.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                evidence_kind=kind,
                subject_id=f"case-{index}",
                dimensions={"cohort": "default"},
                execution_id=f"exec:{join_key}",
                join_key=join_key,
                timestamp=when,
                capability_id="invoice-agent",
                implementation_id="v1",
                model_version=model,
                token_cost_usd=Decimal(cost),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                cost_measurement="measured",
                usage_measurement="measured",
                latency_ms=500,
                compute_time_ms=0,
                event_metadata={},
            )
        )
        db.add(
            OutcomeEvent(
                tenant_id="tenant-a",
                join_key=join_key,
                execution_id="",
                capability_id="invoice-agent",
                implementation_id="v1",
                outcome_type="accepted",
                outcome_payload_json={"accepted": True},
                outcome_value="true",
                occurred_at=when,
                ingested_at=when,
                outcome_timestamp=when,
                provenance="MEASURED",
            )
        )


def test_probabilistic_schedule_reharvests_evidence_instead_of_replaying_snapshot(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    first_run = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        for index in range(30):
            _seed_pair(raw, index=index, when=first_run - timedelta(minutes=index))
        raw.commit()
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        schedule = create_probabilistic_decision_schedule(
            db,
            ProbabilisticDecisionScheduleCreate.model_validate(
                {
                    "evidence_source": {
                        "workload": "invoice-agent",
                        "incumbent_model": "model-a",
                        "candidate_model": "model-b",
                    },
                    "policy": {
                        "min_paired_cases": 30,
                        "candidate_shares": [1.0],
                        "max_critical_error_rate": 0.1,
                        "max_p95_latency_ms": 1000,
                        "require_calibrated_forecast": False,
                    },
                    "interval_minutes": 60,
                    "simulations": 100,
                    "seed": 11,
                }
            ),
            created_by="owner@example.com",
            now=first_run,
        )

        first = run_due_probabilistic_decision_schedules(db, now=first_run)
        _seed_pair(raw, index=30, when=first_run + timedelta(minutes=30))
        raw.commit()
        second = run_due_probabilistic_decision_schedules(db, now=first_run + timedelta(minutes=60))

        records = list(
            raw.scalars(
                select(ProbabilisticMigrationDecisionRecord).order_by(
                    ProbabilisticMigrationDecisionRecord.evaluated_at
                )
            )
        )

    assert schedule.schedule_id.startswith("psch_")
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].decision_id != second[0].decision_id
    assert len(records[0].evidence_json["incumbent"]) == 30
    assert len(records[1].evidence_json["incumbent"]) == 31


def test_probabilistic_schedule_uses_retained_rollout_calibration(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'calibrated-schedule.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        for index in range(30):
            _seed_pair(raw, index=index, when=now - timedelta(minutes=index))
        for metric in (
            "monthly_cost_usd",
            "success_rate",
            "p95_latency_ms",
            "critical_error_rate",
        ):
            for period in range(6):
                raw.add(
                    ForecastCalibrationRecord(
                        tenant_id="tenant-a",
                        forecast_id=f"forecast-{metric}-{period}",
                        verification_id=f"verification-{period}",
                        workload="invoice-agent",
                        incumbent_model="model-a",
                        candidate_model="model-b",
                        metric=metric,
                        predicted_mean=100,
                        predicted_low=90,
                        predicted_high=110,
                        observed=100,
                        observed_at=now - timedelta(days=6 - period),
                    )
                )
        raw.commit()
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        create_probabilistic_decision_schedule(
            db,
            ProbabilisticDecisionScheduleCreate.model_validate(
                {
                    "evidence_source": {
                        "workload": "invoice-agent",
                        "incumbent_model": "model-a",
                        "candidate_model": "model-b",
                    },
                    "policy": {
                        "min_paired_cases": 30,
                        "candidate_shares": [1.0],
                        "max_critical_error_rate": 0.1,
                        "max_p95_latency_ms": 1000,
                        "require_calibrated_forecast": True,
                    },
                    "interval_minutes": 60,
                    "simulations": 100,
                }
            ),
            created_by="owner@example.com",
            now=now,
        )

        decisions = run_due_probabilistic_decision_schedules(db, now=now)

    assert len(decisions) == 1
    assert decisions[0].forecast_readiness.calibration_state == "calibrated"
    assert decisions[0].verdict == "recommend"


def test_cloud_worker_discovers_and_runs_probabilistic_schedules(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'prob-worker.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        create_probabilistic_decision_schedule(
            ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a")),
            ProbabilisticDecisionScheduleCreate.model_validate(
                {
                    "evidence_source": {
                        "workload": "invoice-agent",
                        "incumbent_model": "model-a",
                        "candidate_model": "model-b",
                    },
                    "policy": {"require_calibrated_forecast": False},
                    "interval_minutes": 60,
                    "simulations": 100,
                }
            ),
            created_by="test",
            now=now,
        )
    from zeroth.econ.plane.decisioning import scheduler

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: Session(engine))

    assert _run_due_decision_scans(now=now) == 1
