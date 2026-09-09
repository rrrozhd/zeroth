"""A source-authored repricing/refund ledger must preserve physical attempts."""

from tests.econ.assertions import assert_interval_abstention

from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW, definition, engine as database_engine, execution, outcome, scoped, user,
)
from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.cloud.api import record_execution, record_outcome, router
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.counterfactual.service import run_evaluation
from zeroth.econ.plane.debugger.service import timeline
from zeroth.econ.plane.decisioning.service import _version_from_store
from zeroth.econ.plane.instrumentation.models import ExecutionEvent

engine = database_engine


@pytest.fixture
def client(engine, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield scoped(raw)

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    with TestClient(app, headers={"Authorization": f"Bearer {mint_econ_service_token()}"}) as client:
        yield client


def seed(db):
    definition(db, "invoice", "v1")
    for event_id, charge_id, amount, attempt in [
        ("first", "account:request-1", "1.00", 1),
        ("retry", "account:request-2", "0.20", 2),
    ]:
        record_execution(execution(
            event_id=event_id, charge_id=charge_id, cost_role="charge",
            cost_usd=amount, attempt=attempt, metadata={"provider": "test"},
        ), db, user())
    record_outcome(outcome(), db, user())


def revision(amount="0.70", minute=1, **changes):
    return {
        "charge_id": "account:request-1",
        "asserted_at": (NOW + timedelta(minutes=minute)).isoformat(),
        "token_cost_usd": amount,
        "cost_measurement": "measured",
        "reason": "source billing correction",
        **changes,
    }


def read(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        version = _version_from_store(db, workflow="invoice", version="v1", outcome_type="accepted")
        point = timeline(db, workflow_id="invoice")[0]
        rows = list(db.scalars(select(ExecutionEvent)))
        assert len(rows) == 2
        assert {row.execution_id: row.token_cost_usd for row in rows} == {
            "first": Decimal("1.00"), "retry": Decimal("0.20"),
        }
        assert version.charge_ownership.owned_charge_records == 2
        return version, point


def test_repricing_refund_and_withdrawal_do_not_create_an_attempt(engine, client):
    with Session(engine) as raw:
        seed(scoped(raw))
    # These expectations are the source ledger: one corrected attempt plus its
    # separate $0.20 charged retry. They do not use the production cost helper.
    for amount, expected_total in [("0.70", "0.90"), ("1.25", "1.45"), ("0", "0.20")]:
        minute = {"0.70": 1, "1.25": 2, "0": 3}[amount]
        payload = revision(amount, minute)
        response = client.post("/v1/charge-cost-revisions", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "inserted"
        assert client.post("/v1/charge-cost-revisions", json=payload).json()["status"] == "duplicate"
        version, point = read(engine)
        assert version.runs[0].cost_usd == Decimal(expected_total)
        assert point.measured_cost_usd == float(expected_total)
        assert version.runs[0].accepted is True
    withdrawn = revision(None, 4, cost_measurement="unmeasured")
    assert client.post("/v1/charge-cost-revisions", json=withdrawn).status_code == 200
    version, point = read(engine)
    assert version.runs[0].cost_usd is None
    assert point.measured_cost_usd == 0.20
    assert point.successful_runs == 1  # Cost withdrawal does not change business maturity.


def test_cost_readers_reconcile_the_same_corrected_ledger(engine, client, monkeypatch):
    from zeroth.econ.plane.costing.service import estimate_cost_for_period
    from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
    from zeroth.econ.plane.reconciliation.schemas import ProviderBillImportRequest
    from zeroth.econ.plane.reconciliation.service import import_provider_bill, provider_bill_report

    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        import_provider_bill(db, ProviderBillImportRequest(
            statement_id="corrected-source", provider="test",
            period_start=NOW - timedelta(days=1), period_end=NOW + timedelta(days=1),
            billed_total_usd="0.90", source_kind="manual", buckets=[{
                "bucket_id": "all", "period_start": NOW - timedelta(days=1),
                "period_end": NOW + timedelta(days=1), "amount_usd": "0.90",
            }],
        ))
    assert client.post("/v1/charge-cost-revisions", json=revision()).status_code == 200
    with Session(engine) as raw:
        db = scoped(raw)
        owner = db.scalars(select(ExecutionEvent).where(ExecutionEvent.execution_id == "first")).one()
        report = provider_bill_report(db, provider="test", statement_id="corrected-source")
        assert report.telemetry_measured_usd == Decimal("0.90")
        assert report.telemetry_variance_usd == 0
        assert report.reconciliation_state == "reconciled"
        assert sum(row.event_count for row in report.allocations) == 2
        estimate = estimate_cost_for_period(
            db, owner.capability_id, owner.implementation_id,
            NOW - timedelta(days=1), NOW + timedelta(days=1),
        )
        # Preserve the legacy estimator's explicit 5% overhead, separately from
        # observed charge dollars. Its forecasting claims are not accepted here.
        assert estimate.llm_cost_estimate_usd == Decimal("0.90")
        assert estimate.total_cost_estimate_usd == Decimal("0.945")
        monkeypatch.setattr(settings, "stat_cost_engine", False)
        value = run_evaluation(db, EvaluationRunRequest(
            capability_id=owner.capability_id, implementation_id=owner.implementation_id,
            period_start=NOW - timedelta(days=1), period_end=NOW + timedelta(days=1),
            mode="PROXY_MODEL",
        ))
        assert value.estimated_cost_usd == Decimal("0.90")


def test_revision_source_order_history_and_conflict(engine, client):
    from datetime import UTC, datetime

    with Session(engine) as raw:
        seed(scoped(raw))
    assert client.post("/v1/charge-cost-revisions", json=revision("0.30", 2)).status_code == 200
    assert client.post("/v1/charge-cost-revisions", json=revision("0.70", 1)).status_code == 200
    future = revision("9", asserted_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert client.post("/v1/charge-cost-revisions", json=future).status_code == 200
    version, _ = read(engine)
    assert version.runs[0].cost_usd == Decimal("0.50")
    assert version.source_fingerprint.cost_revision_records == 2
    assert version.source_fingerprint.version == "stored-assertions/5"
    conflict = client.post("/v1/charge-cost-revisions", json=revision("0.80", 2))
    assert conflict.status_code == 422
    assert "immutable charge cost revision" in conflict.json()["detail"]
    history = client.get("/v1/charge-cost-revisions", params={"charge_id": "account:request-1"})
    assert history.status_code == 200
    assert [Decimal(row["token_cost_usd"]) for row in history.json()] == [Decimal("9"), Decimal("0.30"), Decimal("0.70")]


def test_source_erasure_removes_revisions_before_charge_id_reuse(engine, client):
    from sqlalchemy.orm import sessionmaker
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser

    with Session(engine) as raw:
        seed(scoped(raw))
    assert client.post("/v1/charge-cost-revisions", json=revision()).status_code == 200
    eraser = SqlAlchemyEconEventEraser(sessionmaker(bind=engine))
    assert eraser._delete_sync("tenant-a", ["shared"], "erase-revised") == 4
    history = client.get("/v1/charge-cost-revisions", params={"charge_id": "account:request-1"})
    assert history.json() == []
    with Session(engine) as raw:
        seed(scoped(raw))
    version, _ = read(engine)
    assert version.runs[0].cost_usd == Decimal("1.20")


def test_erasure_between_owner_check_and_write_cannot_leave_an_orphan(engine, client):
    from sqlalchemy.orm import sessionmaker
    from tests.econ_plane.test_outcome_ingest_race import _held_sessionmaker
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser
    from zeroth.econ.plane.instrumentation.models import ChargeCostRevisionRecord

    with Session(engine) as raw:
        seed(scoped(raw))
    eraser = SqlAlchemyEconEventEraser(sessionmaker(bind=engine))
    factory = _held_sessionmaker(
        engine, lambda: eraser._delete_sync("tenant-a", ["shared"], "erase-racing"),
    )

    def racing_db():
        with factory() as raw:
            yield scoped(raw)

    client.app.dependency_overrides[get_cloud_scoped_db] = racing_db
    response = client.post("/v1/charge-cost-revisions", json=revision())
    assert response.status_code in {409, 422}, response.text
    with Session(engine) as raw:
        assert list(raw.scalars(select(ChargeCostRevisionRecord))) == []


@pytest.mark.parametrize("amount", ["0.00000001", "9999999999.12345678"])
def test_cost_storage_preserves_declared_precision_and_exact_retries(engine, client, amount):
    with Session(engine) as raw:
        seed(scoped(raw))
    payload = revision(amount)
    first = client.post("/v1/charge-cost-revisions", json=payload)
    assert first.status_code == 200, first.text
    repeat = client.post("/v1/charge-cost-revisions", json=payload)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["status"] == "duplicate"
    rows = client.get("/v1/charge-cost-revisions", params={"charge_id": "account:request-1"}).json()
    assert Decimal(rows[0]["token_cost_usd"]) == Decimal(amount)


@pytest.mark.parametrize("change", [{}, {"token_cost_usd": "0.80"}, {"reason": "different source"}])
def test_racing_revisions_compare_the_winning_assertion(engine, change):
    from sqlalchemy.exc import IntegrityError
    from tests.econ_plane.test_outcome_ingest_race import _held_sessionmaker
    from zeroth.econ.charge_costs import ChargeCostRevision
    from zeroth.econ.plane.instrumentation.charge_costs import ingest_revision
    from zeroth.econ.plane.instrumentation.models import ChargeCostRevisionRecord

    with Session(engine) as raw:
        seed(scoped(raw))

    def winner():
        with Session(engine) as raw:
            assert ingest_revision(scoped(raw), ChargeCostRevision.model_validate(revision()))[0] == "inserted"

    collisions = []
    factory = _held_sessionmaker(engine, winner, lost=collisions)
    with factory() as raw:
        payload = ChargeCostRevision.model_validate(revision(**change))
        if change:
            with pytest.raises(ValueError, match="immutable charge cost revision"):
                ingest_revision(scoped(raw), payload)
        else:
            assert ingest_revision(scoped(raw), payload)[0] == "duplicate"
    assert len(collisions) == 1
    with Session(engine) as raw:
        assert len(list(raw.scalars(select(ChargeCostRevisionRecord)))) == 1


def test_charge_revision_changes_retained_evidence_without_rewriting_history(engine, client):
    from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
    from zeroth.econ.plane.decisioning.service import (
        compare_versions_from_store, retain_decision, list_retained_decisions,
    )

    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        definition(db, "invoice", "v0")
        record_execution(execution(
            version="v0", event_id="baseline", cost_usd="1.00",
            cost_role="charge", charge_id="baseline-charge",
        ), db, user())
        record_outcome(outcome(version="v0"), db, user())
    request = VersionComparisonRequest(
        workflow="invoice", baseline_version="v0", candidate_version="v1",
        policy={"min_runs": 1, "min_success_rate": 0.5, "max_cost_per_outcome_increase": 0},
    )

    def retain():
        with Session(engine) as raw:
            db = scoped(raw)
            return retain_decision(db, request, compare_versions_from_store(db, request), evaluated_by="test")

    before = retain()
    assert_interval_abstention(before)
    assert client.post("/v1/charge-cost-revisions", json=revision()).status_code == 200
    corrected = retain()
    assert_interval_abstention(corrected)
    assert corrected.candidate.measured_cost_usd < before.candidate.measured_cost_usd
    assert corrected.decision_id != before.decision_id
    assert corrected.source_evidence["candidate"].execution_records == 2
    assert corrected.source_evidence["candidate"].cost_revision_records == 1
    assert client.post("/v1/charge-cost-revisions", json=revision(None, 2, cost_measurement="unmeasured")).status_code == 200
    withdrawn = retain()
    assert withdrawn.verdict == "abstain"
    with Session(engine) as raw:
        history = {row.decision_id: row for row in list_retained_decisions(scoped(raw))}
        assert all(history[item.decision_id] == item for item in [before, corrected, withdrawn])


def test_revision_cannot_target_a_missing_or_foreign_tenant_owner(engine, client):
    with Session(engine) as raw:
        db = scoped(raw, "tenant-b")
        record_execution(execution(
            event_id="foreign", charge_id="account:request-1", cost_role="charge", cost_usd="2",
        ), db, user("tenant-b"))
    for charge in ["account:request-1", "missing"]:
        result = client.post("/v1/charge-cost-revisions", json=revision(charge_id=charge))
        assert result.status_code == 422
        assert "existing owned charge" in result.json()["detail"]
    assert client.get("/v1/charge-cost-revisions", params={"charge_id": "account:request-1"}).json() == []


def test_fractional_json_numbers_cannot_silently_round_a_cost_assertion(engine, client):
    import json

    with Session(engine) as raw:
        seed(scoped(raw))
    # Preserve the original JSON token so the test includes HTTP decoding.
    wire = json.dumps(revision()).replace('"0.70"', '9999999999.12345678')
    response = client.post(
        "/v1/charge-cost-revisions", content=wire, headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert "decimal strings" in response.text
