"""Provider-billed dollars reconcile to workflow outcomes without becoming estimates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import time

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from zeroth.econ.plane.debugger.service import create_outcome_definition
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome
from zeroth.econ.plane.reconciliation.api import router as reconciliation_router
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_START = datetime(2026, 8, 1, tzinfo=UTC)
_END = _START + timedelta(days=31)


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


def _client(
    engine, *, tenant_id: str = "tenant-a", roles: list[str] | None = None
) -> TestClient:
    app = FastAPI()
    app.include_router(reconciliation_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: ScopedUserClaims(
        sub="finance-test",
        email="finance@example.com",
        roles=roles or ["Admin"],
        tenant_id=tenant_id,
        exp=int(time()) + 300,
        iss="zeroth-test",
        aud=None,
    )
    return TestClient(app)


def _execution(
    execution_id: str,
    run_id: str,
    *,
    cost: str,
    model: str = "gpt-5",
    workflow_version: str = "v1",
    measurement: MeasurementState = MeasurementState.MEASURED,
    provider_dimensions: dict[str, str] | None = None,
) -> ExecutionEventCreate:
    return ExecutionEventCreate(
        execution_id=execution_id,
        join_key=run_id,
        timestamp=_START + timedelta(days=2),
        capability_id="invoice-processing",
        implementation_id=f"invoice-processing:{workflow_version}",
        model_version=model,
        token_cost_usd=Decimal(cost),
        cost_measurement=measurement,
        workflow_id="invoice-processing",
        workflow_version=workflow_version,
        run_id=run_id,
        step_id="extract",
        metadata={
            "provider": "openai",
            "model": model,
            **(provider_dimensions or {}),
        },
    )


def _seed_resolved_runs(engine) -> None:
    with Session(engine) as raw:
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
        for execution_id, run_id, cost, accepted in (
            ("success-event", "success-run", "0.30", True),
            ("failure-event", "failure-run", "0.20", False),
        ):
            ingest_execution(db, _execution(execution_id, run_id, cost=cost))
            ingest_outcome(
                db,
                OutcomeEventCreate(
                    execution_id=execution_id,
                    join_key=run_id,
                    capability_id="invoice-processing",
                    implementation_id="invoice-processing:v1",
                    outcome_type="approval",
                    outcome_value=accepted,
                    occurred_at=_START + timedelta(days=3),
                ),
            )


def _statement(*, total: str = "0.70") -> dict:
    return {
        "statement_id": "openai-2026-08",
        "provider": "openai",
        "period_start": _START.isoformat(),
        "period_end": _END.isoformat(),
        "currency": "USD",
        "billed_total_usd": total,
        "source_kind": "cost_api",
        "buckets": [
            {
                "bucket_id": "gpt-5",
                "period_start": _START.isoformat(),
                "period_end": _END.isoformat(),
                "amount_usd": "0.60",
                "model": "gpt-5",
                "provider_dimensions": {},
            },
            {
                "bucket_id": "gpt-5-nano",
                "period_start": _START.isoformat(),
                "period_end": _END.isoformat(),
                "amount_usd": "0.10",
                "model": "gpt-5-nano",
                "provider_dimensions": {},
            },
        ],
    }


def test_provider_bill_allocates_billed_money_and_preserves_unreconciled_variance(
    econ_engine,
) -> None:
    _seed_resolved_runs(econ_engine)
    client = _client(econ_engine)

    imported = client.post("/v1/reconciliation/provider-bills", json=_statement())
    report_response = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    )

    assert imported.status_code == 201
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["reconciliation_state"] == "unreconciled"
    assert Decimal(report["billed_total_usd"]) == Decimal("0.70")
    assert Decimal(report["allocated_billed_usd"]) == Decimal("0.60")
    assert Decimal(report["unreconciled_billed_usd"]) == Decimal("0.10")
    assert Decimal(report["telemetry_measured_usd"]) == Decimal("0.50")
    assert Decimal(report["telemetry_variance_usd"]) == Decimal("0.20")
    assert Decimal(report["outcome_unresolved_usd"]) == Decimal("0")
    assert report["matched_buckets"] == 1
    assert report["unmatched_buckets"] == [
        {"bucket_id": "gpt-5-nano", "reason": "no_measured_telemetry"}
    ]
    assert [
        (
            row["workflow_id"],
            row["workflow_version"],
            row["outcome_status"],
            Decimal(row["billed_cost_usd"]),
            Decimal(row["telemetry_cost_usd"]),
        )
        for row in report["allocations"]
    ] == [
        ("invoice-processing", "v1", "failure", Decimal("0.24"), Decimal("0.20")),
        ("invoice-processing", "v1", "success", Decimal("0.36"), Decimal("0.30")),
    ]
    assert report["allocation_method"] == "measured_cost_proportional"


def test_provider_bill_exact_replay_is_idempotent_and_changed_content_is_rejected(
    econ_engine,
) -> None:
    client = _client(econ_engine)
    original = _statement()

    created = client.post("/v1/reconciliation/provider-bills", json=original)
    replay = client.post("/v1/reconciliation/provider-bills", json=original)
    changed = client.post(
        "/v1/reconciliation/provider-bills",
        json={
            **original,
            "billed_total_usd": "0.71",
            "buckets": [
                *original["buckets"][:-1],
                {**original["buckets"][-1], "amount_usd": "0.11"},
            ],
        },
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["statement_digest"] == created.json()["statement_digest"]
    assert changed.status_code == 409
    assert changed.json() == {
        "detail": "Provider bill is immutable for this provider and statement_id"
    }
    assert _client(econ_engine, tenant_id="tenant-b").get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).status_code == 404
    assert _client(econ_engine, roles=["Analyst"]).get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).status_code == 200
    assert _client(econ_engine, roles=["Viewer"]).get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).status_code == 403


def test_provider_bill_requires_a_closed_source_total_and_admin_authority(
    econ_engine,
) -> None:
    malformed = _statement(total="0.71")

    rejected = _client(econ_engine).post(
        "/v1/reconciliation/provider-bills", json=malformed
    )
    forbidden = _client(econ_engine, roles=["Analyst"]).post(
        "/v1/reconciliation/provider-bills", json=_statement()
    )

    assert rejected.status_code == 422
    assert "must equal the sum of buckets" in rejected.text
    assert forbidden.status_code == 403


def test_provider_bill_rejects_statement_ids_that_cannot_be_used_in_report_urls(
    econ_engine,
) -> None:
    invalid = {**_statement(), "statement_id": "openai/2026-08"}

    rejected = _client(econ_engine).post(
        "/v1/reconciliation/provider-bills", json=invalid
    )

    assert rejected.status_code == 422
    assert "statement_id" in rejected.text


def test_provider_bill_does_not_use_estimated_cost_as_allocation_weight(econ_engine) -> None:
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _execution(
                "estimated-event",
                "estimated-run",
                cost="0.70",
                measurement=MeasurementState.ESTIMATED,
            ),
        )

    client = _client(econ_engine)
    assert client.post("/v1/reconciliation/provider-bills", json=_statement()).status_code == 201
    report = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).json()

    assert Decimal(report["allocated_billed_usd"]) == Decimal("0")
    assert Decimal(report["unreconciled_billed_usd"]) == Decimal("0.70")
    assert Decimal(report["telemetry_measured_usd"]) == Decimal("0")
    assert report["reconciliation_state"] == "unreconciled"


def test_provider_bill_reports_outcome_semantics_as_a_separate_closure_gate(
    econ_engine,
) -> None:
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _execution(
                "v2-event",
                "v2-run",
                cost="0.50",
                workflow_version="v2",
            ),
        )
    body = _statement(total="0.50")
    body["buckets"] = [
        {**body["buckets"][0], "amount_usd": "0.50"},
    ]
    client = _client(econ_engine)

    assert client.post("/v1/reconciliation/provider-bills", json=body).status_code == 201
    report = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).json()

    assert report["reconciliation_state"] == "outcomes_unresolved"
    assert Decimal(report["unreconciled_billed_usd"]) == Decimal("0")
    assert Decimal(report["outcome_unresolved_usd"]) == Decimal("0.50")
    assert report["allocations"][0]["outcome_status"] == "unresolved"


@pytest.mark.parametrize(
    ("billed", "expected_state", "expected_variance"),
    [
        ("0.50", "reconciled", "0"),
        ("0.60", "allocated_with_variance", "0.10"),
    ],
)
def test_provider_bill_distinguishes_exact_closure_from_allocated_variance(
    econ_engine, billed: str, expected_state: str, expected_variance: str
) -> None:
    _seed_resolved_runs(econ_engine)
    body = _statement(total=billed)
    body["buckets"] = [{**body["buckets"][0], "amount_usd": billed}]
    client = _client(econ_engine)

    assert client.post("/v1/reconciliation/provider-bills", json=body).status_code == 201
    report = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).json()

    assert report["reconciliation_state"] == expected_state
    assert Decimal(report["allocated_billed_usd"]) == Decimal(billed)
    assert Decimal(report["unreconciled_billed_usd"]) == Decimal("0")
    assert Decimal(report["telemetry_variance_usd"]) == Decimal(expected_variance)


def test_overlapping_provider_buckets_fail_closed_instead_of_double_allocating(
    econ_engine,
) -> None:
    _seed_resolved_runs(econ_engine)
    body = _statement(total="0.50")
    body["buckets"] = [
        {**body["buckets"][0], "bucket_id": "overlap-a", "amount_usd": "0.25"},
        {**body["buckets"][0], "bucket_id": "overlap-b", "amount_usd": "0.25"},
    ]
    client = _client(econ_engine)

    assert client.post("/v1/reconciliation/provider-bills", json=body).status_code == 201
    report = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).json()

    assert report["reconciliation_state"] == "unreconciled"
    assert Decimal(report["allocated_billed_usd"]) == Decimal("0")
    assert report["allocations"] == []
    assert report["unmatched_buckets"] == [
        {"bucket_id": "overlap-a", "reason": "ambiguous_bucket_scope"},
        {"bucket_id": "overlap-b", "reason": "ambiguous_bucket_scope"},
    ]


def test_provider_bill_migration_is_tenant_bound_and_independently_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'provider-bills.db'}"
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    config = _migration_config(url)
    command.upgrade(config, "head")
    engine = create_engine(url, future=True)
    try:
        inspector = inspect(engine)
        assert {"provider_bills", "provider_cost_buckets"} <= set(
            inspector.get_table_names()
        )
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("provider_bills")
        } >= {
            "uq_provider_bills_tenant_id",
            "uq_provider_bills_tenant_provider_statement",
        }
        foreign_keys = inspector.get_foreign_keys("provider_cost_buckets")
        assert any(
            key["constrained_columns"] == ["tenant_id", "provider_bill_id"]
            and key["referred_columns"] == ["tenant_id", "id"]
            for key in foreign_keys
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "20260901_17"
    finally:
        engine.dispose()

    command.downgrade(config, "20260830_12")
    engine = create_engine(url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "provider_bills" not in tables
        assert "provider_cost_buckets" not in tables
        assert "outcome_definitions" in tables
    finally:
        engine.dispose()


def test_provider_dimensions_scope_allocation_and_expose_unbilled_telemetry(
    econ_engine,
) -> None:
    with Session(econ_engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        ingest_execution(
            db,
            _execution(
                "project-a-event",
                "project-a-run",
                cost="0.30",
                provider_dimensions={"project_id": "proj_a"},
            ),
        )
        ingest_execution(
            db,
            _execution(
                "project-b-event",
                "project-b-run",
                cost="0.20",
                provider_dimensions={"project_id": "proj_b"},
            ),
        )
    body = _statement(total="0.30")
    body["buckets"] = [
        {
            **body["buckets"][0],
            "amount_usd": "0.30",
            "provider_dimensions": {"project_id": "proj_a"},
        }
    ]
    client = _client(econ_engine)

    assert client.post("/v1/reconciliation/provider-bills", json=body).status_code == 201
    report = client.get(
        "/v1/reconciliation/provider-bills/openai/openai-2026-08/report"
    ).json()

    assert Decimal(report["telemetry_measured_usd"]) == Decimal("0.30")
    assert Decimal(report["unbilled_telemetry_usd"]) == Decimal("0.20")
    assert Decimal(report["allocated_billed_usd"]) == Decimal("0.30")
    assert report["allocations"][0]["bucket_id"] == "gpt-5"
    assert report["allocations"][0]["model"] == "gpt-5"
    assert report["allocations"][0]["provider_dimensions"] == {
        "project_id": "proj_a"
    }
