"""Contract tests for the economic-debugger evidence spine and queries."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import time

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.service.economic_diagnostic_cli import render_markdown
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane import database as database_module
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from zeroth.econ.plane.debugger.service import create_outcome_definition
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


_NOW = datetime(2026, 8, 30, tzinfo=UTC)


def test_debugger_migration_columns_are_part_of_the_startup_convergence_gate() -> None:
    debugger_columns = {
        ("execution_events", name, "20260830_11")
        for name in (
            "workflow_id",
            "workflow_version",
            "run_id",
            "step_id",
            "attempt",
            "subject_id",
            "dimensions",
        )
    }

    assert debugger_columns <= set(database_module._CHAIN_OWNED_COLUMNS)


def _migration_config(database_url: str) -> Config:
    root = Path(__file__).parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def econ_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("event_type", [ExecutionEvent, ExecutionEventCreate])
def test_execution_contract_preserves_the_debugger_identity_spine(event_type) -> None:
    event = event_type(
        execution_id="event-1",
        timestamp=_NOW,
        capability_id="invoice-processing",
        implementation_id="invoice-processing:v3",
        model_version="gpt-5-mini",
        token_cost_usd=Decimal("0.12"),
        workflow_id="invoice-processing",
        workflow_version="v3",
        run_id="run-7",
        step_id="extract",
        attempt=2,
        subject_id="customer-42",
        dimensions={"plan": "enterprise", "priority": 3, "trial": False},
    )

    dumped = event.model_dump()
    assert dumped["workflow_id"] == "invoice-processing"
    assert dumped["workflow_version"] == "v3"
    assert dumped["run_id"] == "run-7"
    assert dumped["step_id"] == "extract"
    assert dumped["attempt"] == 2
    assert dumped["subject_id"] == "customer-42"
    assert dumped["dimensions"] == {
        "plan": "enterprise",
        "priority": 3,
        "trial": False,
    }


@pytest.mark.parametrize("event_type", [ExecutionEvent, ExecutionEventCreate])
def test_execution_contract_rejects_unbounded_or_nested_dimensions(event_type) -> None:
    base = {
        "execution_id": "event-1",
        "timestamp": _NOW,
        "capability_id": "invoice-processing",
        "implementation_id": "invoice-processing:v3",
        "model_version": "gpt-5-mini",
    }

    with pytest.raises(ValidationError, match="at most 16"):
        event_type(**base, dimensions={f"key_{index}": index for index in range(17)})

    with pytest.raises(ValidationError):
        event_type(**base, dimensions={"nested": {"unsafe": "shape"}})


def test_execution_ingestion_persists_and_idempotently_compares_the_spine(econ_engine) -> None:
    payload = ExecutionEventCreate(
        execution_id="event-1",
        join_key="run-7",
        timestamp=_NOW,
        capability_id="invoice-processing",
        implementation_id="invoice-processing:v3",
        model_version="gpt-5-mini",
        token_cost_usd=Decimal("0.12"),
        workflow_id="invoice-processing",
        workflow_version="v3",
        run_id="run-7",
        step_id="extract",
        attempt=2,
        subject_id="customer-42",
        dimensions={"plan": "enterprise"},
    )

    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        status, row = ingest_execution(db, payload)
        duplicate_status, duplicate = ingest_execution(db, payload)

        assert status == "inserted"
        assert duplicate_status == "duplicate"
        assert duplicate.id == row.id
        assert row.workflow_id == "invoice-processing"
        assert row.workflow_version == "v3"
        assert row.run_id == "run-7"
        assert row.step_id == "extract"
        assert row.attempt == 2
        assert row.subject_id == "customer-42"
        assert row.dimensions == {"plan": "enterprise"}

        with pytest.raises(ValueError, match="workflow_version"):
            ingest_execution(db, payload.model_copy(update={"workflow_version": "v4"}))


def _event(
    execution_id: str,
    run_id: str,
    step_id: str,
    subject_id: str,
    *,
    cost: str,
    attempt: int = 1,
    measurement: MeasurementState = MeasurementState.MEASURED,
    plan: str = "enterprise",
    capability_id: str = "invoice-processing",
    implementation_id: str = "invoice-processing:v1",
    workflow_version: str = "v1",
) -> ExecutionEventCreate:
    return ExecutionEventCreate(
        execution_id=execution_id,
        join_key=run_id,
        timestamp=_NOW,
        capability_id=capability_id,
        implementation_id=implementation_id,
        model_version="gpt-5-mini",
        token_cost_usd=Decimal(cost),
        cost_measurement=measurement,
        workflow_id="invoice-processing",
        workflow_version=workflow_version,
        run_id=run_id,
        step_id=step_id,
        attempt=attempt,
        subject_id=subject_id,
        dimensions={"plan": plan},
    )


def _seed_debugger_fixture(engine, tenant_id: str = "tenant-a") -> None:
    capability_id = f"{tenant_id}:invoice-processing"
    implementation_id = f"{tenant_id}:invoice-processing:v1"
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))
        create_outcome_definition(
            db,
            OutcomeDefinitionCreate(
                workflow_id="invoice-processing",
                workflow_version="v1",
                outcome_type="approval",
                operator="equals",
                target=True,
            ),
        )
        events = [
            _event(
                "success-extract",
                "run-success",
                "extract",
                "customer-a",
                cost="0.10",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
            _event(
                "success-verify",
                "run-success",
                "verify",
                "customer-a",
                cost="0.20",
                measurement=MeasurementState.ESTIMATED,
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
            _event(
                "failure-extract-1",
                "run-failed",
                "extract",
                "customer-b",
                cost="0.30",
                plan="free",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
            _event(
                "failure-extract-2",
                "run-failed",
                "extract",
                "customer-b",
                cost="0.10",
                attempt=2,
                plan="free",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
        ]
        for event in events:
            ingest_execution(db, event)
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id="success-verify",
                join_key="run-success",
                capability_id=capability_id,
                implementation_id=implementation_id,
                outcome_type="approval",
                outcome_value=True,
                occurred_at=_NOW,
            ),
        )
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id="failure-extract-2",
                join_key="run-failed",
                capability_id=capability_id,
                implementation_id=implementation_id,
                outcome_type="approval",
                outcome_value=False,
                occurred_at=_NOW,
            ),
        )


def _client(
    engine, tenant_id: str = "tenant-a", roles: list[str] | None = None
) -> TestClient:
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: ScopedUserClaims(
        sub="debugger-test",
        email="debugger@example.com",
        roles=roles or ["Viewer"],
        tenant_id=tenant_id,
        exp=int(time()) + 300,
        iss="zeroth-test",
    )
    return TestClient(app)


def test_timeline_reconciles_cost_outcomes_and_measurement_channels(econ_engine) -> None:
    _seed_debugger_fixture(econ_engine)

    response = _client(econ_engine).get(
        "/v1/debugger/timeline", params={"workflow_id": "invoice-processing"}
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "period_start": "2026-08-30T00:00:00",
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "runs": 2,
            "successful_runs": 1,
            "failed_runs": 1,
            "measured_cost_usd": 0.5,
            "estimated_cost_usd": 0.2,
            "measured_failure_exposure_usd": 0.4,
            "estimated_failure_exposure_usd": 0.0,
            "measured_cost_per_successful_outcome_usd": 0.5,
            "estimated_cost_per_successful_outcome_usd": 0.2,
            "incomplete_events": 0,
        }
    ]


def test_cohorts_compare_subjects_and_typed_dimensions(econ_engine) -> None:
    _seed_debugger_fixture(econ_engine)
    client = _client(econ_engine)

    subjects = client.get(
        "/v1/debugger/cohorts",
        params={"workflow_id": "invoice-processing", "group_by": "subject_id"},
    )
    plans = client.get(
        "/v1/debugger/cohorts",
        params={
            "workflow_id": "invoice-processing",
            "group_by": "dimension",
            "dimension": "plan",
        },
    )

    assert subjects.status_code == 200
    assert [(row["cohort"], row["successful_runs"]) for row in subjects.json()] == [
        ("customer-a", 1),
        ("customer-b", 0),
    ]
    assert plans.status_code == 200
    assert [(row["cohort"], row["failed_runs"]) for row in plans.json()] == [
        ("enterprise", 0),
        ("free", 1),
    ]


def test_breakage_reports_failed_run_exposure_without_claiming_step_causality(
    econ_engine,
) -> None:
    _seed_debugger_fixture(econ_engine)

    response = _client(econ_engine).get(
        "/v1/debugger/breakage", params={"workflow_id": "invoice-processing"}
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "step_id": "extract",
            "failed_runs": 1,
            "measured_failure_exposure_usd": 0.4,
            "estimated_failure_exposure_usd": 0.0,
            "measured_repeated_attempt_cost_usd": 0.1,
            "estimated_repeated_attempt_cost_usd": 0.0,
            "attribution": "failed_run_exposure_not_step_causality",
        }
    ]


def test_diagnostic_report_turns_evidence_into_one_honest_next_action(econ_engine) -> None:
    _seed_debugger_fixture(econ_engine)

    response = _client(econ_engine).get(
        "/v1/debugger/report",
        params={"workflow_id": "invoice-processing", "cohort_dimension": "plan"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "workflow_id": "invoice-processing",
        "window_start": None,
        "window_end": None,
        "cohort_dimension": "plan",
        "claim_scope": "observed_economic_exposure",
        "decision_state": "economic_risk_observed",
        "data_quality": "mixed_cost_evidence",
        "event_count": 4,
        "runs": 2,
        "successful_runs": 1,
        "failed_runs": 1,
        "unresolved_runs": 0,
        "undefined_outcome_versions": [],
        "outcome_coverage": 1.0,
        "measured_events": 3,
        "estimated_events": 1,
        "unmeasured_events": 0,
        "incomplete_events": 0,
        "measured_cost_usd": 0.5,
        "estimated_cost_usd": 0.2,
        "measured_failure_exposure_usd": 0.4,
        "estimated_failure_exposure_usd": 0.0,
        "measured_cost_per_successful_outcome_usd": 0.5,
        "estimated_cost_per_successful_outcome_usd": 0.2,
        "top_failure_exposure": {
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "step_id": "extract",
            "failed_runs": 1,
            "measured_failure_exposure_usd": 0.4,
            "estimated_failure_exposure_usd": 0.0,
            "measured_repeated_attempt_cost_usd": 0.1,
            "estimated_repeated_attempt_cost_usd": 0.0,
            "attribution": "failed_run_exposure_not_step_causality",
        },
        "highest_failure_rate_cohort": {
            "cohort": "free",
            "runs": 1,
            "successful_runs": 0,
            "failed_runs": 1,
            "measured_cost_usd": 0.4,
            "estimated_cost_usd": 0.0,
            "measured_cost_per_successful_outcome_usd": None,
            "estimated_cost_per_successful_outcome_usd": None,
            "incomplete_events": 0,
        },
        "recommended_action": {
            "code": "investigate_retry_policy",
            "rationale": "$0.10000000 measured cost came from explicit repeated attempts in failed runs.",
            "supported_claim": "Repeated-attempt cost is observed; whether changing retries preserves outcomes is unproven.",
        },
        "limitations": [
            "Failed-run exposure identifies where money accumulated, not which step caused the failure.",
            "Estimated cost is kept separate and is not provider-billed ground truth.",
            "Outcome success follows an immutable workflow-version definition; undefined versions remain unresolved.",
            "This report observes production history; it does not prove savings from an untested change.",
        ],
    }


def test_seeded_evidence_renders_as_a_claim_bounded_markdown_artifact(econ_engine) -> None:
    _seed_debugger_fixture(econ_engine)

    response = _client(econ_engine).get(
        "/v1/debugger/report",
        params={"workflow_id": "invoice-processing", "cohort_dimension": "plan"},
    )
    artifact = render_markdown(response.json())

    assert response.status_code == 200
    assert "**Decision state:** economic risk observed" in artifact
    assert "| Measured failed-run exposure | $0.40000000 |" in artifact
    assert "`free` has a 100.0% failure rate" in artifact
    assert "whether changing retries preserves outcomes is unproven" in artifact
    assert "not which step caused the failure" in artifact
    assert "savings opportunity" not in artifact.lower()


def test_diagnostic_report_refuses_a_decision_when_outcomes_are_missing(econ_engine) -> None:
    capability_id = "tenant-a:invoice-processing"
    implementation_id = "tenant-a:invoice-processing:v1"
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        create_outcome_definition(
            db,
            OutcomeDefinitionCreate(
                workflow_id="invoice-processing",
                workflow_version="v1",
                outcome_type="approval",
                operator="equals",
                target=True,
            ),
        )
        ingest_execution(
            db,
            _event(
                "unresolved",
                "run-unresolved",
                "extract",
                "customer-a",
                cost="0.10",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
        )

    response = _client(econ_engine).get(
        "/v1/debugger/report", params={"workflow_id": "invoice-processing"}
    )

    assert response.status_code == 200
    report = response.json()
    assert report["decision_state"] == "insufficient_evidence"
    assert report["unresolved_runs"] == 1
    assert report["outcome_coverage"] == 0.0
    assert report["recommended_action"]["code"] == "instrument_outcomes"


def test_diagnostic_report_does_not_infer_success_without_an_outcome_definition(
    econ_engine,
) -> None:
    capability_id = "tenant-a:invoice-processing"
    implementation_id = "tenant-a:invoice-processing:v1"
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _event(
                "ambiguous-outcome",
                "run-ambiguous",
                "extract",
                "customer-a",
                cost="0.10",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
        )
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id="ambiguous-outcome",
                join_key="run-ambiguous",
                capability_id=capability_id,
                implementation_id=implementation_id,
                outcome_type="custom",
                outcome_value=True,
                occurred_at=_NOW,
            ),
        )

    report = _client(econ_engine).get(
        "/v1/debugger/report", params={"workflow_id": "invoice-processing"}
    ).json()

    assert report["decision_state"] == "insufficient_evidence"
    assert report["successful_runs"] == 0
    assert report["failed_runs"] == 0
    assert report["unresolved_runs"] == 1
    assert report["undefined_outcome_versions"] == ["v1"]
    assert report["recommended_action"]["code"] == "define_outcome_success"


def test_versioned_outcome_definition_controls_business_success(econ_engine) -> None:
    capability_id = "tenant-a:invoice-processing"
    implementation_id = "tenant-a:invoice-processing:v1"
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _event(
                "fraud-outcome",
                "run-fraud",
                "screen",
                "customer-a",
                cost="0.10",
                capability_id=capability_id,
                implementation_id=implementation_id,
            ),
        )
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id="fraud-outcome",
                join_key="run-fraud",
                capability_id=capability_id,
                implementation_id=implementation_id,
                outcome_type="fraud_flag",
                outcome_value=True,
                occurred_at=_NOW,
            ),
        )

    definition = _client(econ_engine, roles=["Admin"]).post(
        "/v1/debugger/outcome-definitions",
        json={
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "outcome_type": "fraud_flag",
            "operator": "equals",
            "target": False,
        },
    )
    report = _client(econ_engine).get(
        "/v1/debugger/report", params={"workflow_id": "invoice-processing"}
    ).json()

    assert definition.status_code == 201
    assert report["successful_runs"] == 0
    assert report["failed_runs"] == 1
    assert report["unresolved_runs"] == 0
    assert report["decision_state"] == "economic_risk_observed"


def test_outcome_definition_is_immutable_within_a_workflow_version(econ_engine) -> None:
    client = _client(econ_engine, roles=["Admin"])
    original = {
        "workflow_id": "invoice-processing",
        "workflow_version": "v1",
        "outcome_type": "approval",
        "operator": "equals",
        "target": True,
    }

    created = client.post("/v1/debugger/outcome-definitions", json=original)
    replay = client.post("/v1/debugger/outcome-definitions", json=original)
    changed = client.post(
        "/v1/debugger/outcome-definitions",
        json={**original, "target": False},
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["definition_digest"] == created.json()["definition_digest"]
    assert changed.status_code == 409
    assert changed.json() == {
        "detail": "Outcome definition is immutable for this workflow version"
    }
    assert client.get(
        "/v1/debugger/outcome-definitions",
        params={"workflow_id": "invoice-processing"},
    ).json() == [created.json()]
    assert _client(econ_engine, tenant_id="tenant-b").get(
        "/v1/debugger/outcome-definitions",
        params={"workflow_id": "invoice-processing"},
    ).json() == []


def test_numeric_outcome_definition_applies_a_versioned_threshold(econ_engine) -> None:
    capability_id = "tenant-a:invoice-processing"
    implementation_id = "tenant-a:invoice-processing:v2"
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _event(
                "reopen-outcome",
                "run-reopened",
                "review",
                "customer-a",
                cost="0.10",
                capability_id=capability_id,
                implementation_id=implementation_id,
                workflow_version="v2",
            ),
        )
        ingest_outcome(
            db,
            OutcomeEventCreate(
                execution_id="reopen-outcome",
                join_key="run-reopened",
                capability_id=capability_id,
                implementation_id=implementation_id,
                outcome_type="reopen_rate",
                outcome_value=0.10,
                occurred_at=_NOW,
            ),
        )

    client = _client(econ_engine, roles=["Admin"])
    created = client.post(
        "/v1/debugger/outcome-definitions",
        json={
            "workflow_id": "invoice-processing",
            "workflow_version": "v2",
            "outcome_type": "reopen_rate",
            "operator": "less_than_or_equal",
            "target": 0.05,
        },
    )
    report = client.get(
        "/v1/debugger/report", params={"workflow_id": "invoice-processing"}
    ).json()

    assert created.status_code == 201
    assert report["failed_runs"] == 1
    assert report["successful_runs"] == 0
    assert report["undefined_outcome_versions"] == []


def test_diagnostic_report_returns_404_instead_of_a_zero_value_story(econ_engine) -> None:
    response = _client(econ_engine).get(
        "/v1/debugger/report", params={"workflow_id": "missing-workflow"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "No economic evidence found for this workflow and window"}


def test_debugger_queries_are_bound_to_the_authenticated_tenant(econ_engine) -> None:
    _seed_debugger_fixture(econ_engine, "tenant-a")
    _seed_debugger_fixture(econ_engine, "tenant-b")

    response = _client(econ_engine, "tenant-a").get(
        "/v1/debugger/timeline", params={"workflow_id": "invoice-processing"}
    )

    assert response.status_code == 200
    assert response.json()[0]["runs"] == 2
    assert response.json()[0]["measured_cost_usd"] == 0.5


def test_debugger_spine_migration_backfills_existing_execution_identity(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "econ.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260824_10')"))
        connection.execute(
            text(
                "CREATE TABLE execution_events ("
                "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), execution_id VARCHAR(128), "
                "join_key VARCHAR(128), timestamp DATETIME, capability_id VARCHAR(128), "
                "implementation_id VARCHAR(128), metadata JSON)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO execution_events VALUES "
                "(1, 'tenant-a', 'event-1', 'run-1', '2026-08-30', "
                "'invoice-processing', 'invoice-processing:v1', '{}')"
            )
        )
    engine.dispose()

    command.upgrade(_migration_config(url), "head")

    engine = create_engine(url, future=True)
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("execution_events")
        }
        assert "outcome_definitions" in inspect(engine).get_table_names()
        assert {
            "workflow_id",
            "workflow_version",
            "run_id",
            "step_id",
            "attempt",
            "subject_id",
            "dimensions",
        } <= columns
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "20260901_16"
            )
            identity = connection.execute(
                text(
                    "SELECT workflow_id, workflow_version, run_id, attempt, dimensions "
                    "FROM execution_events WHERE id = 1"
                )
            ).one()
        assert identity[:4] == (
            "invoice-processing",
            "invoice-processing:v1",
            "run-1",
            1,
        )
        assert identity[4] in ({}, "{}")
    finally:
        engine.dispose()
