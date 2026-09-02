from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.evidence import harvest_migration_evidence
from zeroth.econ.plane.decisioning.schemas import MigrationEvidenceSource
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _seed_run(
    db: Session,
    *,
    tenant: str,
    case: str,
    model: str,
    cost: str,
    accepted: bool,
    cohort: str,
    when: datetime,
) -> None:
    join_key = f"{case}:{model}"
    db.add(
        ExecutionEvent(
            tenant_id=tenant,
            evidence_kind="production" if model == "model-a" else "synthetic_control",
            subject_id=case,
            dimensions={"cohort": cohort},
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
            tenant_id=tenant,
            join_key=join_key,
            execution_id="",
            capability_id="invoice-agent",
            implementation_id="v1",
            outcome_type="accepted",
            outcome_payload_json={"accepted": accepted, "critical_error": not accepted},
            outcome_value=str(accepted).lower(),
            occurred_at=when,
            ingested_at=when,
            outcome_timestamp=when,
            provenance="MEASURED",
        )
    )


def test_harvester_builds_paired_cohort_evidence_from_current_tenant_telemetry(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'harvest.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        for index in range(3):
            cohort = "enterprise" if index == 0 else "self-serve"
            _seed_run(
                raw,
                tenant="tenant-a",
                case=f"case-{index}",
                model="model-a",
                cost="1.00",
                accepted=True,
                cohort=cohort,
                when=now - timedelta(days=index),
            )
            _seed_run(
                raw,
                tenant="tenant-a",
                case=f"case-{index}",
                model="model-b",
                cost="0.40",
                accepted=index != 2,
                cohort=cohort,
                when=now - timedelta(days=index),
            )
        _seed_run(
            raw,
            tenant="tenant-b",
            case="foreign",
            model="model-b",
            cost="99",
            accepted=False,
            cohort="enterprise",
            when=now,
        )
        raw.commit()

    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        result = harvest_migration_evidence(
            db,
            MigrationEvidenceSource(
                workload="invoice-agent",
                incumbent_model="model-a",
                candidate_model="model-b",
                outcome_type="accepted",
                lookback_days=30,
            ),
            now=now,
        )

    assert result.gaps == []
    assert result.evidence is not None
    assert [row.case_id for row in result.evidence.incumbent] == [
        "case-0",
        "case-1",
        "case-2",
    ]
    assert [row.cohort for row in result.evidence.incumbent] == [
        "enterprise",
        "self-serve",
        "self-serve",
    ]
    assert result.evidence.candidate[-1].accepted is False
    assert result.evidence.candidate[-1].critical_error is True
    assert result.evidence.period_request_counts == [1, 1, 1]
    assert result.lineage.paired_cases == 3
    assert result.lineage.collected_at == now


def test_harvester_refuses_aggregate_backtests_and_reports_missing_pair_level_data(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'insufficient.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        result = harvest_migration_evidence(
            db,
            MigrationEvidenceSource(
                workload="invoice-agent",
                incumbent_model="model-a",
                candidate_model="model-b",
            ),
            now=datetime(2026, 9, 2, tzinfo=UTC),
        )

    assert result.evidence is None
    assert result.gaps == ["no_paired_case_level_evidence"]
    assert result.lineage.paired_cases == 0


def test_harvester_reads_an_explicit_case_level_artifact_from_a_retained_backtest(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backtest-artifact.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 2, tzinfo=UTC)

    def rows(model: str, cost: str) -> list[dict[str, object]]:
        return [
            {
                "case_id": f"case-{index}",
                "cohort": "enterprise",
                "cost_usd": cost,
                "latency_ms": 500,
                "accepted": True,
                "critical_error": False,
                "source": f"backtest:{model}",
            }
            for index in range(30)
        ]

    with Session(engine) as raw:
        raw.add(
            EconomicBacktestRecord(
                backtest_id="bkt_1",
                tenant_id="tenant-a",
                request_digest="digest",
                workflow="invoice-agent",
                baseline_version="v1",
                node_id="extract",
                incumbent_model="model-a",
                candidate_model="model-b",
                verdict="pass",
                provider_call_credits=60,
                report_json={
                    "incumbent_observations": rows("model-a", "1.00"),
                    "candidate_observations": rows("model-b", "0.50"),
                    "period_request_counts": [90, 100, 110],
                },
                period_start=now,
                evaluated_at=now,
                evaluated_by="test",
            )
        )
        raw.commit()
        result = harvest_migration_evidence(
            ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a")),
            MigrationEvidenceSource(
                workload="invoice-agent",
                incumbent_model="model-a",
                candidate_model="model-b",
            ),
            now=now,
        )

    assert result.evidence is not None
    assert len(result.evidence.candidate) == 30
    assert result.evidence.period_request_counts == [90, 100, 110]
    assert result.lineage.sources == ["backtest:model-a", "backtest:model-b"]
