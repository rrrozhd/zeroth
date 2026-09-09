from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.evidence import harvest_migration_evidence
from zeroth.econ.plane.decisioning.schemas import MigrationEvidenceSource
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

NOW = datetime(2026, 9, 2, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as raw:
        yield raw
    engine.dispose()


def _seed(db, model, run, *, recorded_run=True, shared_join=False, steps=1, accepted=True):
    join = run if shared_join else f"{run}:{model}"
    for step in range(steps):
        db.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id=f"{join}:{model}:{step}",
                join_key=join,
                run_id=run if recorded_run else None,
                subject_id="customer-1",
                model_version=model,
                implementation_id=f"impl-{model}",
                capability_id="w",
                timestamp=NOW,
                token_cost_usd=Decimal("1"),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                latency_ms=100,
                cost_measurement="measured",
                usage_measurement="measured",
                dimensions={"cohort": "same"},
                evidence_kind="production",
            )
        )
    db.add(
        OutcomeEvent(
            tenant_id="tenant-a",
            execution_id="",
            join_key=join,
            capability_id="w",
            implementation_id=f"impl-{model}",
            outcome_type="accepted",
            outcome_value=str(accepted).lower(),
            outcome_payload_json={"accepted": accepted, "critical_error": not accepted},
            occurred_at=NOW,
            ingested_at=NOW,
            outcome_timestamp=NOW,
            provenance="MEASURED",
        )
    )


def _harvest(db):
    db.commit()
    db.expunge_all()
    return harvest_migration_evidence(
        ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a")),
        MigrationEvidenceSource(workload="w", incumbent_model="a", candidate_model="b"),
        now=NOW,
    )


def test_repeated_subject_runs_remain_separate_pairs(db):
    for run in ("run-1", "run-2", "run-3"):
        _seed(db, "a", run)
        _seed(db, "b", run)
    result = _harvest(db)
    assert result.evidence is not None, result.gaps
    assert result.lineage.paired_cases == 3
    assert [row.cost_usd for row in result.evidence.incumbent] == [Decimal("1")] * 3
    assert result.evidence.period_request_counts == [3]


def test_missing_run_pair_abstains_instead_of_merging_subject(db):
    for run in ("run-1", "run-2", "run-3"):
        _seed(db, "a", run)
    _seed(db, "b", "run-1")
    result = _harvest(db)
    assert result.evidence is None
    assert "paired_outcomes_missing" in result.gaps


def test_ambiguous_legacy_subject_pairing_abstains(db):
    _seed(db, "a", "run-1", recorded_run=False)
    _seed(db, "a", "run-2", recorded_run=False)
    _seed(db, "b", "run-1", recorded_run=False)
    result = _harvest(db)
    assert result.evidence is None
    assert "ambiguous_run_pairing" in result.gaps


def test_shared_join_key_cannot_cross_implementation_outcomes(db):
    _seed(db, "a", "run-1", shared_join=True, accepted=True)
    _seed(db, "b", "run-1", shared_join=True, accepted=False)
    result = _harvest(db)
    assert result.evidence is not None, result.gaps
    assert result.evidence.incumbent[0].accepted is True
    assert result.evidence.incumbent[0].critical_error is False
    assert result.evidence.candidate[0].accepted is False
    assert result.evidence.candidate[0].critical_error is True


def test_steps_of_one_recorded_run_still_sum_within_run(db):
    _seed(db, "a", "run-1", steps=3)
    _seed(db, "b", "run-1", steps=2)
    result = _harvest(db)
    assert result.evidence is not None, result.gaps
    assert result.lineage.paired_cases == 1
    assert result.evidence.incumbent[0].cost_usd == Decimal("3")
    assert result.evidence.incumbent[0].latency_ms == 300
    assert result.evidence.candidate[0].cost_usd == Decimal("2")


def test_incomplete_legacy_run_cannot_hide_subject_ambiguity(db):
    _seed(db, "a", "run-1", recorded_run=False)
    _seed(db, "a", "run-2", recorded_run=False)
    _seed(db, "b", "run-1", recorded_run=False)
    for row in db.new:
        if isinstance(row, ExecutionEvent) and row.join_key == "run-2:a":
            row.cost_measurement = "unmeasured"
    result = _harvest(db)
    assert result.evidence is None
    assert "ambiguous_run_pairing" in result.gaps


def test_run_and_subject_identifier_strings_are_not_interchangeable(db):
    _seed(db, "a", "run-1")
    _seed(db, "b", "legacy-join", recorded_run=False)
    for row in db.new:
        if isinstance(row, ExecutionEvent) and row.model_version == "b":
            row.subject_id = "run-1"
    result = _harvest(db)
    assert result.evidence is None
    assert result.gaps


def test_latest_outcome_selected_by_time_not_insertion_order(db):
    _seed(db, "a", "run-1", accepted=True)
    _seed(db, "b", "run-1", accepted=True)
    db.flush()
    earlier = NOW - timedelta(hours=1)
    db.add(
        OutcomeEvent(
            tenant_id="tenant-a",
            execution_id="",
            join_key="run-1:a",
            capability_id="w",
            implementation_id="impl-a",
            outcome_type="accepted",
            outcome_value="false",
            outcome_payload_json={"accepted": False},
            occurred_at=earlier,
            ingested_at=NOW,
            outcome_timestamp=earlier,
            provenance="MEASURED",
        )
    )
    result = _harvest(db)
    assert result.evidence is not None, result.gaps
    assert result.evidence.incumbent[0].accepted is True
