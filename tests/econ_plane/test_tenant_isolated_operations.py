from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.connectors.models import (
    ConnectorConfig,
    ConnectorDeliveryLog,
    ConnectorOutbox,
)
from zeroth.econ.plane.connectors.api import retry_outbox as retry_outbox_api
from zeroth.econ.plane.connectors.service import (
    list_connector_configs,
    list_outbox,
    render_prometheus_metrics,
    retry_outbox_item,
)
from zeroth.econ.plane.connectors.workers import process_connector_outbox
from zeroth.econ.plane.costing.models import PricingCatalog
from zeroth.econ.plane.costing.service import PricingCatalogReader, estimate_cost_for_period
from zeroth.econ.plane.counterfactual.api import router as counterfactual_router
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.counterfactual.service import run_evaluation
from zeroth.econ.plane.counterfactual.models import ValueEstimate, ValuationRun
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.enforcement.models import PolicyAction
from zeroth.econ.plane.enforcement.schemas import EnforcementActionCreate
from zeroth.econ.plane.enforcement.service import create_action
from zeroth.econ.plane.performance.models import PerformanceSnapshot  # noqa: F401
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 11, tzinfo=UTC)


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


def _scope(engine, tenant_id: str) -> tuple[Session, ScopedSession]:
    session = Session(engine)
    return session, ScopedSession(session, TenantWideScopeContext(tenant_id=tenant_id))


def _seed_capabilities(engine) -> None:
    with Session(engine) as db:
        db.add_all(
            [
                Capability(id="cap-a", tenant_id="tenant-a", name="A"),
                Capability(id="cap-b", tenant_id="tenant-b", name="B"),
                Implementation(
                    id="impl-a",
                    tenant_id="tenant-a",
                    capability_id="cap-a",
                    name="A implementation",
                ),
                Implementation(
                    id="impl-b",
                    tenant_id="tenant-b",
                    capability_id="cap-b",
                    name="B implementation",
                ),
            ]
        )
        db.commit()


def _execution(
    execution_id: str,
    capability_id: str,
    implementation_id: str,
    *,
    tenant_id: str | None = None,
) -> ExecutionEventCreate:
    return ExecutionEventCreate(
        tenant_id=tenant_id,
        execution_id=execution_id,
        timestamp=_NOW,
        capability_id=capability_id,
        implementation_id=implementation_id,
        model_version="v1",
        token_cost_usd=Decimal("1"),
    )


def test_create_action_uses_the_bound_tenant_for_its_policy_row(econ_engine) -> None:
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        action = create_action(
            tenant_a,
            EnforcementActionCreate(
                capability_id="cap-a",
                action_type="TriggerInvestigation",
                reason="review",
            ),
        )

        policy = tenant_a.scalars(select(PolicyAction)).one()
        assert action.tenant_id == "tenant-a"
        assert policy.tenant_id == "tenant-a"
    finally:
        raw.close()


def test_cost_estimate_reads_global_pricing_through_a_separate_scope(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    raw_tenant, tenant_a = _scope(econ_engine, "tenant-a")
    raw_global = Session(econ_engine)
    global_pricing = ScopedSession(raw_global, None)
    try:
        global_pricing.add(
            PricingCatalog(
                provider="provider-a",
                model="model-a",
                effective_from=_NOW,
                input_per_million_usd=2.0,
                output_per_million_usd=4.0,
            )
        )
        global_pricing.commit()
        ingest_execution(
            tenant_a,
            _execution("inferred-cost", "cap-a", "impl-a").model_copy(
                update={
                    "token_cost_usd": Decimal("0"),
                    "metadata": {
                        "provider": "provider-a",
                        "model": "model-a",
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 500_000,
                    },
                }
            ),
        )

        estimate = estimate_cost_for_period(
            tenant_a,
            "cap-a",
            "impl-a",
            _NOW,
            _NOW,
            pricing=PricingCatalogReader(global_pricing),
        )

        assert estimate.tenant_id == "tenant-a"
        assert estimate.data_quality == "inferred"
        assert float(estimate.llm_cost_estimate_usd) > 0
    finally:
        raw_tenant.close()
        raw_global.close()


def test_cross_tenant_duplicate_execution_id_returns_only_bound_row(econ_engine) -> None:
    """A01-14: an execution identifier is unique only inside its tenant."""
    _seed_capabilities(econ_engine)
    raw_a, tenant_a = _scope(econ_engine, "tenant-a")
    raw_b, tenant_b = _scope(econ_engine, "tenant-b")
    try:
        status_a, row_a = ingest_execution(tenant_a, _execution("same-id", "cap-a", "impl-a"))
        status_b, row_b = ingest_execution(tenant_b, _execution("same-id", "cap-b", "impl-b"))

        assert (status_a, row_a.tenant_id) == ("inserted", "tenant-a")
        assert (status_b, row_b.tenant_id) == ("inserted", "tenant-b")
        assert row_a.id != row_b.id

        duplicate_status, duplicate_b = ingest_execution(
            tenant_b, _execution("same-id", "cap-b", "impl-b")
        )
        assert duplicate_status == "duplicate"
        assert duplicate_b.id == row_b.id
        assert duplicate_b.tenant_id == "tenant-b"
    finally:
        raw_a.close()
        raw_b.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("capability_id", "cap-other"),
        ("implementation_id", "impl-other"),
        ("join_key", "other-join"),
        ("timestamp", datetime(2026, 8, 12, tzinfo=UTC)),
        ("model_version", "v2"),
        ("token_cost_usd", Decimal("2")),
        ("tool_cost_usd", Decimal("2")),
        ("compute_cost_usd", Decimal("2")),
        ("latency_ms", 10),
        ("compute_time_ms", 20),
        ("metadata", {"request_id": "other-request"}),
    ],
)
def test_conflicting_execution_replay_rejects_without_mutating_original(
    econ_engine, field, replacement
) -> None:
    _seed_capabilities(econ_engine)
    with Session(econ_engine) as seed:
        seed.add_all(
            [
                Capability(id="cap-other", tenant_id="tenant-a", name="Other"),
                Implementation(
                    id="impl-other",
                    tenant_id="tenant-a",
                    capability_id="cap-other",
                    name="Other implementation",
                ),
            ]
        )
        seed.commit()
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        original = _execution("replayed", "cap-a", "impl-a")
        original.join_key = "original-join"
        original.metadata = {"request_id": "original-request"}
        status, row = ingest_execution(tenant_a, original)
        assert status == "inserted"

        replay = original.model_copy(update={field: replacement})
        with pytest.raises(ValueError, match="conflicting immutable fields"):
            ingest_execution(tenant_a, replay)

        raw.expire_all()
        unchanged = raw.get(ExecutionEvent, row.id)
        assert unchanged is not None
        assert unchanged.capability_id == "cap-a"
        assert unchanged.implementation_id == "impl-a"
        assert unchanged.join_key == "original-join"
        assert unchanged.model_version == "v1"
        assert unchanged.token_cost_usd == Decimal("1")
        assert unchanged.event_metadata["request_id"] == "original-request"
    finally:
        raw.close()


def test_exact_execution_replay_is_duplicate(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        payload = _execution("exact-replay", "cap-a", "impl-a")
        payload.join_key = "exact-join"
        payload.metadata = {"request_id": "exact-request"}
        _, original = ingest_execution(tenant_a, payload)

        status, replay = ingest_execution(tenant_a, payload.model_copy(deep=True))

        assert status == "duplicate"
        assert replay.id == original.id
    finally:
        raw.close()


def test_capability_owner_required_for_ingestion_and_implementation_scope(
    econ_engine,
) -> None:
    """A01-32: ownership comes from the bound scope, not payload identifiers."""
    _seed_capabilities(econ_engine)
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        with pytest.raises(ValueError, match="capability.*bound tenant"):
            ingest_execution(tenant_a, _execution("foreign-cap", "cap-b", "impl-b"))

        with pytest.raises(ValueError, match="implementation.*capability"):
            ingest_execution(tenant_a, _execution("wrong-impl", "cap-a", "impl-b"))

        with pytest.raises(ValueError, match="tenant.*bound scope"):
            ingest_execution(
                tenant_a,
                _execution("payload-owner", "cap-a", "impl-a", tenant_id="tenant-b"),
            )
    finally:
        raw.close()


def test_outcome_links_only_to_execution_in_bound_scope(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    raw_a, tenant_a = _scope(econ_engine, "tenant-a")
    raw_b, tenant_b = _scope(econ_engine, "tenant-b")
    try:
        ingest_execution(tenant_a, _execution("shared", "cap-a", "impl-a"))
        ingest_execution(tenant_b, _execution("shared", "cap-b", "impl-b"))

        row = ingest_outcome(
            tenant_b,
            OutcomeEventCreate(
                execution_id="shared",
                capability_id="cap-b",
                implementation_id="impl-b",
                outcome_type="conversion",
                outcome_value=True,
            ),
        )

        assert row.tenant_id == "tenant-b"
        assert row.capability_id == "cap-b"
        assert row.join_key == "shared"
        visible = tenant_b.scalars(select(OutcomeEvent)).all()
        assert [item.id for item in visible] == [row.id]
    finally:
        raw_a.close()
        raw_b.close()


def test_linked_outcome_derives_implementation_and_is_evaluated(econ_engine, monkeypatch) -> None:
    _seed_capabilities(econ_engine)
    monkeypatch.setattr(
        "zeroth.econ.plane.instrumentation.service.settings.connectors_enabled", False
    )
    monkeypatch.setattr(
        "zeroth.econ.plane.counterfactual.service.settings.connectors_enabled", False
    )
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        ingest_execution(tenant_a, _execution("derived-implementation", "cap-a", "impl-a"))
        outcome = ingest_outcome(
            tenant_a,
            OutcomeEventCreate(
                execution_id="derived-implementation",
                capability_id="cap-a",
                outcome_type="conversion",
                outcome_value=True,
                occurred_at=_NOW,
            ),
        )

        assert outcome.implementation_id == "impl-a"
        estimate = run_evaluation(
            tenant_a,
            EvaluationRunRequest(
                capability_id="cap-a",
                implementation_id="impl-a",
                mode="PROXY_MODEL",
                period_start=_NOW,
                period_end=_NOW,
            ),
        )
        assert estimate.method_metadata["sample_size"] == 1
        assert float(estimate.estimated_value_usd) == pytest.approx(120.0)
    finally:
        raw.close()


def test_linked_outcome_rejects_explicit_implementation_mismatch(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    with Session(econ_engine) as seed:
        seed.add(
            Implementation(
                id="impl-alt",
                tenant_id="tenant-a",
                capability_id="cap-a",
                name="Alternate implementation",
            )
        )
        seed.commit()
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        ingest_execution(tenant_a, _execution("mismatch", "cap-a", "impl-a"))
        with pytest.raises(ValueError, match="does not match execution"):
            ingest_outcome(
                tenant_a,
                OutcomeEventCreate(
                    execution_id="mismatch",
                    capability_id="cap-a",
                    implementation_id="impl-alt",
                    outcome_type="conversion",
                ),
            )
    finally:
        raw.close()


def test_counterfactual_api_maps_expected_domain_errors_to_404(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    with Session(econ_engine) as seed:
        seed.add(
            Implementation(
                id="impl-other-capability",
                tenant_id="tenant-a",
                capability_id="cap-a",
                name="Other capability implementation",
            )
        )
        seed.commit()

    app = FastAPI()
    app.include_router(counterfactual_router, prefix="/v1")

    def scoped_db():
        with Session(econ_engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-b"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: ScopedUserClaims(
        sub="analyst",
        email="analyst@example.com",
        roles=["Analyst"],
        tenant_id="tenant-b",
        exp=2_000_000_000,
        iss="test",
    )
    client = TestClient(app)
    payload = {
        "capability_id": "missing",
        "mode": "PROXY_MODEL",
        "period_start": _NOW.isoformat(),
        "period_end": _NOW.isoformat(),
    }

    missing = client.post("/v1/evaluations/run", json=payload)
    mismatch = client.post(
        "/v1/evaluations/run",
        json={**payload, "capability_id": "cap-b", "implementation_id": "impl-a"},
    )

    assert (missing.status_code, missing.json()) == (
        404,
        {"detail": "capability does not exist in the bound tenant"},
    )
    assert (mismatch.status_code, mismatch.json()) == (
        404,
        {"detail": "implementation does not belong to the capability in the bound tenant"},
    )


def test_connector_worker_drains_each_eligible_tenant_scope(econ_engine, monkeypatch) -> None:
    now = datetime.now(UTC)
    with Session(econ_engine) as seed:
        seed.add_all(
            [
                ConnectorOutbox(
                    tenant_id=tenant_id,
                    event_type="execution.event",
                    event_key=f"event-{tenant_id}",
                    payload_json={},
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
                for tenant_id in ("tenant-a", "tenant-b")
            ]
        )
        seed.commit()
    monkeypatch.setattr(
        "zeroth.econ.plane.connectors.workers.SessionLocal",
        sessionmaker(bind=econ_engine, class_=Session),
    )
    monkeypatch.setattr("zeroth.econ.plane.connectors.service.settings.connectors_enabled", True)
    monkeypatch.setattr("zeroth.econ.plane.config.settings.service_principal_tenant_id", "tenant-a")

    assert process_connector_outbox.fn(batch_size=10) == 2

    with Session(econ_engine) as verify:
        rows = verify.execute(select(ConnectorOutbox).order_by(ConnectorOutbox.tenant_id)).scalars()
        assert [(row.tenant_id, row.status) for row in rows] == [
            ("tenant-a", "SENT"),
            ("tenant-b", "SENT"),
        ]


def test_evaluation_rejects_capability_outside_bound_scope(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        with pytest.raises(ValueError, match="capability.*bound tenant"):
            run_evaluation(
                tenant_a,
                EvaluationRunRequest(
                    capability_id="cap-b",
                    mode="PROXY_MODEL",
                    period_start=_NOW,
                    period_end=_NOW,
                ),
            )
    finally:
        raw.close()


def test_evaluation_uses_only_bound_tenant_executions_and_outcomes(
    econ_engine, monkeypatch
) -> None:
    _seed_capabilities(econ_engine)
    monkeypatch.setattr(
        "zeroth.econ.plane.counterfactual.service.settings.connectors_enabled", False
    )
    with Session(econ_engine) as seed:
        seed.add_all(
            [
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id="a",
                    join_key="shared-join",
                    timestamp=_NOW,
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    model_version="v1",
                ),
                ExecutionEvent(
                    tenant_id="tenant-b",
                    execution_id="b",
                    join_key="shared-join",
                    timestamp=_NOW,
                    capability_id="cap-b",
                    implementation_id="impl-b",
                    model_version="v1",
                ),
                OutcomeEvent(
                    tenant_id="tenant-a",
                    execution_id="a",
                    join_key="shared-join",
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    outcome_type="conversion",
                    outcome_value="1",
                    outcome_payload_json={"value": True},
                    occurred_at=_NOW,
                    ingested_at=_NOW,
                    outcome_timestamp=_NOW,
                ),
                OutcomeEvent(
                    tenant_id="tenant-b",
                    execution_id="b",
                    join_key="shared-join",
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    outcome_type="conversion",
                    outcome_value="0",
                    outcome_payload_json={"value": False},
                    occurred_at=_NOW,
                    ingested_at=_NOW,
                    outcome_timestamp=_NOW,
                ),
            ]
        )
        seed.commit()

    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        estimate = run_evaluation(
            tenant_a,
            EvaluationRunRequest(
                capability_id="cap-a",
                implementation_id="impl-a",
                mode="PROXY_MODEL",
                period_start=_NOW,
                period_end=_NOW,
            ),
        )
        assert estimate.tenant_id == "tenant-a"
        assert estimate.method_metadata["sample_size"] == 1
        assert float(estimate.estimated_value_usd) == pytest.approx(120.0)
    finally:
        raw.close()


def test_evaluation_filters_outcomes_to_requested_implementation(econ_engine, monkeypatch) -> None:
    _seed_capabilities(econ_engine)
    monkeypatch.setattr(
        "zeroth.econ.plane.counterfactual.service.settings.connectors_enabled", False
    )
    with Session(econ_engine) as seed:
        seed.add(
            Implementation(
                id="impl-alt",
                tenant_id="tenant-a",
                capability_id="cap-a",
                name="Alternate A implementation",
            )
        )
        seed.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id="execution-a",
                join_key="shared-implementation-join",
                timestamp=_NOW,
                capability_id="cap-a",
                implementation_id="impl-a",
                model_version="v1",
            )
        )
        for implementation_id in ("impl-a", "impl-alt"):
            seed.add(
                OutcomeEvent(
                    tenant_id="tenant-a",
                    execution_id="execution-a",
                    join_key="shared-implementation-join",
                    capability_id="cap-a",
                    implementation_id=implementation_id,
                    outcome_type="conversion",
                    outcome_value="1",
                    outcome_payload_json={"value": True},
                    occurred_at=_NOW,
                    ingested_at=_NOW,
                    outcome_timestamp=_NOW,
                )
            )
        seed.commit()

    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        estimate = run_evaluation(
            tenant_a,
            EvaluationRunRequest(
                capability_id="cap-a",
                implementation_id="impl-a",
                mode="PROXY_MODEL",
                period_start=_NOW,
                period_end=_NOW,
            ),
        )
        assert estimate.method_metadata["sample_size"] == 1
        assert float(estimate.estimated_value_usd) == pytest.approx(120.0)

        all_implementations = run_evaluation(
            tenant_a,
            EvaluationRunRequest(
                capability_id="cap-a",
                mode="PROXY_MODEL",
                period_start=_NOW,
                period_end=_NOW,
            ),
        )
        assert all_implementations.method_metadata["sample_size"] == 2
        assert float(all_implementations.estimated_value_usd) == pytest.approx(240.0)
    finally:
        raw.close()


def test_connector_configuration_requires_bound_tenant(econ_engine) -> None:
    """A01-29: connector selection cannot choose a different tenant."""
    with Session(econ_engine) as seed:
        seed.add_all(
            [
                ConnectorConfig(
                    tenant_id="tenant-a",
                    connector_type="posthog",
                    enabled=False,
                    config_json={"api_key": "a"},
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
                ConnectorConfig(
                    tenant_id="tenant-b",
                    connector_type="posthog",
                    enabled=False,
                    config_json={"api_key": "b"},
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
            ]
        )
        seed.commit()

    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        assert [row.tenant_id for row in list_connector_configs(tenant_a, "tenant-a")] == [
            "tenant-a"
        ]
        with pytest.raises(ValueError, match="tenant.*bound scope"):
            list_connector_configs(tenant_a, "tenant-b")
    finally:
        raw.close()


def test_foreign_outbox_id_is_observably_identical_to_unknown(econ_engine) -> None:
    """A01-26: listing and retrying outbox rows stay inside the bound scope."""
    with Session(econ_engine) as seed:
        foreign = ConnectorOutbox(
            tenant_id="tenant-b",
            event_type="execution.event",
            event_key="foreign",
            payload_json={},
            status="FAILED",
            attempts=1,
            next_attempt_at=_NOW,
            created_at=_NOW,
        )
        seed.add(foreign)
        seed.commit()
        foreign_id = foreign.id

    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        assert list_outbox(tenant_a) == []
        assert retry_outbox_item(tenant_a, foreign_id) is None
        assert retry_outbox_item(tenant_a, foreign_id + 100_000) is None

        with pytest.raises(HTTPException) as foreign_error:
            retry_outbox_api(foreign_id, tenant_a, object())  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as unknown_error:
            retry_outbox_api(foreign_id + 100_000, tenant_a, object())  # type: ignore[arg-type]
        assert (foreign_error.value.status_code, foreign_error.value.detail) == (
            unknown_error.value.status_code,
            unknown_error.value.detail,
        )
    finally:
        raw.close()


def test_operational_services_reject_raw_session(econ_engine) -> None:
    _seed_capabilities(econ_engine)
    with Session(econ_engine) as db:
        with pytest.raises(TypeError, match="ScopedSession"):
            ingest_execution(db, _execution("raw", "cap-a", "impl-a"))
        with pytest.raises(TypeError, match="ScopedSession"):
            list_outbox(db)
        with pytest.raises(TypeError, match="ScopedSession"):
            run_evaluation(
                db,
                EvaluationRunRequest(
                    capability_id="cap-a",
                    mode="PROXY_MODEL",
                    period_start=_NOW,
                    period_end=_NOW,
                ),
            )


def test_metrics_aggregates_only_bound_tenant(econ_engine) -> None:
    with Session(econ_engine) as seed:
        seed.add_all(
            [
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id="visible",
                    join_key="visible",
                    timestamp=_NOW,
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    model_version="v1",
                ),
                ExecutionEvent(
                    tenant_id="tenant-b",
                    execution_id="hidden",
                    join_key="hidden",
                    timestamp=_NOW,
                    capability_id="cap-b",
                    implementation_id="impl-b",
                    model_version="v1",
                ),
            ]
        )
        seed.commit()

    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        rendered = render_prometheus_metrics(tenant_a)
        assert "ecp_execution_events_total 1" in rendered
    finally:
        raw.close()


def test_execution_model_uses_composite_uniqueness_without_owner_defaults() -> None:
    constraints = {
        tuple(constraint.columns.keys()) for constraint in ExecutionEvent.__table__.constraints
    }
    assert ("tenant_id", "execution_id") in constraints
    assert ("execution_id",) not in constraints

    for model in (
        ExecutionEvent,
        OutcomeEvent,
        ValuationRun,
        ValueEstimate,
        ConnectorConfig,
        ConnectorOutbox,
        ConnectorDeliveryLog,
    ):
        column = model.__table__.c.tenant_id
        assert column.default is None
        assert column.server_default is None
        assert column.nullable is False
