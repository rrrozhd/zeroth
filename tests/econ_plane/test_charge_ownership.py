"""Single monetary ownership across capture layers, retries and tenant boundaries."""

from tests.econ.assertions import assert_interval_abstention

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from fastapi import HTTPException
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW,
    definition,
    engine as database_engine,
    execution,
    scoped,
    user,
)
from zeroth.econ.plane.cloud.api import record_execution
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent
from zeroth.econ.plane.costing import models as costing_models  # noqa: F401
from zeroth.econ.plane.decisioning.service import _version_from_store
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.instrumentation import service as ingest

engine = database_engine


@pytest.mark.parametrize("role", ["charge", "summary"])
def test_declared_cost_roles_bind_public_sdk_run_and_step(engine, role):
    with Session(engine) as raw:
        db = scoped(raw)
        event = execution(
            run="public-run",
            step="public-step",
            event_id="event",
            cost_role=role,
            charge_id="charge" if role == "charge" else None,
            cost_usd=None,
            metadata={"run_id": "shadow-run", "node_id": "shadow-step"},
        )
        record_execution(event, db, user())
        row = db.scalars(select(ExecutionEvent)).one()
        assert (row.run_id, row.step_id) == ("public-run", "public-step")


def charge(event_id="provider", charge_id="account:request-a", **changes):
    return execution(event_id=event_id, cost_role="charge", charge_id=charge_id, **changes)


@pytest.mark.parametrize("reverse", [False, True])
def test_same_charge_cannot_be_owned_by_two_capture_events(engine, reverse):
    events = [charge("provider"), charge("framework")]
    if reverse:
        events.reverse()
    with Session(engine) as raw:
        db = scoped(raw)
        assert record_execution(events[0], db, user()).status == "inserted"
        assert record_execution(events[0], db, user()).status == "duplicate"
        with pytest.raises(HTTPException) as error:
            record_execution(events[1], db, user())
        assert error.value.status_code == 422
        assert "charge_id" in error.value.detail
        evidence = _version_from_store(
            db, workflow="invoice", version="v1", outcome_type="accepted"
        )
        assert evidence.runs[0].cost_usd == Decimal("0.10")
        assert len(list(db.scalars(select(ExecutionEvent)))) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"charge_id": "another-charge"},
        {
            "cost_role": "summary",
            "charge_id": None,
            "cost_usd": None,
            "cost_measurement": "unmeasured",
        },
        {"cost_usd": "0.20"},
    ],
)
def test_an_existing_execution_cannot_reassign_ownership_or_money(engine, changes):
    original = charge()
    changed = SdkExecutionEvent.model_validate({**original.model_dump(), **changes})
    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(original, db, user())
        with pytest.raises(HTTPException) as error:
            record_execution(changed, db, user())
        assert error.value.status_code == 422
        owner = db.scalars(select(ExecutionEvent)).one()
        assert (owner.cost_role, owner.charge_id, owner.token_cost_usd) == (
            "charge",
            "account:request-a",
            Decimal("0.10"),
        )
        assert record_execution(original, db, user()).status == "duplicate"


def test_rejected_duplicate_owner_remains_visible_as_inventory_loss(engine):
    from tests.econ_plane.test_source_inventory import (
        deliver,
        reference_digest,
        request_payload,
    )
    from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
    from zeroth.econ.plane.decisioning.service import compare_versions_from_store

    payload = request_payload()
    run = payload["source_windows"]["candidate"]["runs"][0]
    run["execution_count"] = 2
    run["execution_ids_digest"] = reference_digest(["v2-0", "framework"])
    request = VersionComparisonRequest.model_validate(payload)
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            for i in range(2):
                deliver(db, version, i, cost_role="charge", charge_id=f"{version}-{i}")
        duplicate = charge("framework", "v2-0", version="v2", run="run-0", source_window_id="batch")
        with pytest.raises(HTTPException) as error:
            record_execution(duplicate, db, user())
        assert error.value.status_code == 422
        report = compare_versions_from_store(db, request)
        assert report.source_delivery["candidate"].status == "mismatch"
        assert report.source_delivery["candidate"].mismatched_runs == 1
        assert report.verdict == "abstain"
        assert report.candidate.unmeasured_runs == 1
        # Delivering the genuinely structural capture repairs telemetry delivery,
        # without adding money or replacing the declared owner.
        summary = SdkExecutionEvent.model_validate(
            {
                **duplicate.model_dump(),
                "cost_role": "summary",
                "charge_id": None,
                "cost_usd": None,
                "cost_measurement": "unmeasured",
            }
        )
        record_execution(summary, db, user())
        report = compare_versions_from_store(db, request)
        assert report.source_delivery["candidate"].status == "matched"
        assert report.candidate.measured_cost_usd == Decimal("0.20")
        assert report.charge_ownership["candidate"].owned_charge_records == 2
        assert report.charge_ownership["candidate"].summary_records == 1


@pytest.mark.parametrize("summary_first", [False, True])
def test_summaries_do_not_duplicate_or_obscure_child_costs(engine, summary_first):
    events = [
        charge("child-a", "request-a", cost_usd="1"),
        charge("child-b", "request-b", cost_usd="2", attempt=2),
    ]
    summary = execution(
        event_id="parent",
        cost_role="summary",
        cost_usd=None,
        metadata={"prompt_tokens": 100000, "provider": "openai"},
    )
    events.insert(0 if summary_first else len(events), summary)
    with Session(engine) as raw:
        db = scoped(raw)
        for event in events:
            record_execution(event, db, user())
        evidence = _version_from_store(
            db, workflow="invoice", version="v1", outcome_type="accepted"
        )
        assert len(evidence.runs) == 1
        assert evidence.runs[0].cost_usd == Decimal("3")
        assert evidence.charge_ownership.status == "declared"
        assert evidence.charge_ownership.owned_charge_records == 2
        assert evidence.charge_ownership.summary_records == 1
        assert evidence.charge_ownership.unattributed_records == 0


def test_charge_ids_are_scoped_by_tenant_but_not_workflow_or_run(engine):
    for tenant in ["tenant-a", "tenant-b"]:
        with Session(engine) as raw:
            db = scoped(raw, tenant)
            record_execution(charge(), db, user(tenant))
            with pytest.raises(HTTPException) as error:
                record_execution(
                    charge("other", workflow="another-workflow", run="other-run"), db, user(tenant)
                )
            assert error.value.status_code == 422
            # Distinct provider/account/attempt identities must never collapse by timing/amount.
            record_execution(charge("other-account", "account-b:request-a"), db, user(tenant))
            record_execution(charge("retry", "account:request-b", attempt=2), db, user(tenant))
            evidence = _version_from_store(
                db, workflow="invoice", version="v1", outcome_type="accepted"
            )
            assert evidence.runs[0].cost_usd == Decimal("0.30")


@pytest.mark.parametrize("kind", ["summary", "unknown_charge", "zero_charge", "legacy"])
def test_unknown_and_zero_cost_keep_distinct_meanings(engine, kind):
    event = {
        "summary": lambda: execution(event_id="parent", cost_role="summary", cost_usd=None),
        "unknown_charge": lambda: charge(cost_usd=None),
        "zero_charge": lambda: charge(cost_usd="0"),
        "legacy": lambda: execution(cost_usd="1"),
    }[kind]()
    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(event, db, user())
        evidence = _version_from_store(
            db, workflow="invoice", version="v1", outcome_type="accepted"
        )
        expected = (
            None
            if kind in {"summary", "unknown_charge"}
            else Decimal("0" if kind == "zero_charge" else "1")
        )
        assert len(evidence.runs) == 1
        assert evidence.runs[0].cost_usd == expected
        assert evidence.charge_ownership.status == (
            "unverified" if kind in {"legacy", "summary"} else "declared"
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"cost_role": "summary", "cost_usd": "1"},
        {"cost_role": "summary", "cost_usd": "0"},
        {"cost_role": "summary", "cost_usd": None, "charge_id": "charge"},
        {"cost_role": "charge"},
        {"cost_role": "legacy_unknown", "charge_id": "charge"},
        {"cost_role": "charge", "charge_id": ""},
        {"cost_role": "charge", "charge_id": "x" * 129},
    ],
)
def test_incoherent_monetary_ownership_is_rejected(payload):
    with pytest.raises(ValidationError):
        SdkExecutionEvent.model_validate(
            {"workflow": "invoice", "run_id": "run", "step": "step", **payload}
        )


def test_concurrent_distinct_executions_cannot_claim_one_charge(engine, monkeypatch):
    # Finish registry creation before racing the charge constraint.
    with Session(engine) as raw:
        record_execution(execution(event_id="setup", run="setup"), scoped(raw), user())
    barrier = Barrier(2)
    original = ingest._stage_execution

    def race(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "_stage_execution", race)

    def write(event_id):
        with Session(engine) as raw:
            try:
                return record_execution(charge(event_id), scoped(raw), user()).status
            except HTTPException as error:
                assert error.status_code == 422
                assert "charge_id" in error.detail
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, value) for value in ("first", "second")]
        assert sorted(f.result(timeout=20) for f in futures) == ["conflict", "inserted"]
    with Session(engine) as raw:
        rows = list(
            scoped(raw).scalars(
                select(ExecutionEvent).where(ExecutionEvent.execution_id != "setup")
            )
        )
        assert len(rows) == 1


def test_summary_token_metadata_never_becomes_a_second_price_estimate(engine):
    from datetime import timedelta
    from zeroth.econ.plane.costing.service import estimate_cost_for_period

    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(charge(cost_usd="1"), db, user())
        record_execution(
            execution(
                event_id="summary",
                cost_role="summary",
                cost_usd=None,
                metadata={"provider": "openai", "prompt_tokens": 1000000},
            ),
            db,
            user(),
        )
        owner = db.scalars(
            select(ExecutionEvent).where(ExecutionEvent.execution_id == "provider")
        ).one()
        estimate = estimate_cost_for_period(
            db,
            owner.capability_id,
            owner.implementation_id,
            NOW,
            NOW + timedelta(days=1),
            pricing=None,
        )
        assert Decimal(str(estimate.total_cost_estimate_usd)) == Decimal("1.05")
        assert estimate.data_quality == "measured"


def test_debugger_preserves_summary_only_runs_as_incomplete(engine):
    from zeroth.econ.plane.debugger.service import diagnostic_report, timeline, cohorts

    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(
            execution(
                event_id="summary",
                cost_role="summary",
                cost_usd=None,
                metadata={"node_id": "parent"},
            ),
            db,
            user(),
        )
        report = diagnostic_report(db, workflow_id="invoice")
        assert report.runs == 1
        assert report.data_quality == "incomplete"
        assert report.decision_state == "insufficient_evidence"
        assert report.unmeasured_events == 0
        assert report.summary_events == report.incomplete_events == 1
        assert timeline(db, workflow_id="invoice")[0].incomplete_events == 1
        assert cohorts(db, workflow_id="invoice", group_by="subject_id")[0].incomplete_events == 1
        record_execution(charge(metadata={"node_id": "model"}), db, user())
        report = diagnostic_report(db, workflow_id="invoice")
        assert report.runs == 1
        assert report.measured_cost_usd == 0.1
        assert report.unmeasured_events == report.incomplete_events == 0
        assert (
            report.event_count
            == report.measured_events
            + report.estimated_events
            + report.unmeasured_events
            + report.summary_events
        )
        assert timeline(db, workflow_id="invoice")[0].incomplete_events == 0


def test_retained_comparison_binds_ownership_and_preserves_legacy_uncertainty(engine):
    from zeroth.econ.plane.cloud.api import record_outcome
    from tests.econ_plane.test_sdk_evidence_namespace import outcome
    from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
    from zeroth.econ.plane.decisioning.service import (
        compare_versions_from_store,
        retain_decision,
        list_retained_decisions,
    )

    request = VersionComparisonRequest(
        workflow="invoice",
        baseline_version="v1",
        candidate_version="v2",
        policy={"min_runs": 1, "min_success_rate": 0.5},
    )
    with Session(engine) as raw:
        db = scoped(raw)
        for version, cost in [("v1", "2"), ("v2", "1")]:
            definition(db, "invoice", version)
            record_execution(charge(version, version, version=version, cost_usd=cost), db, user())
            record_outcome(outcome(version=version), db, user())

        def compare():
            return retain_decision(
                db, request, compare_versions_from_store(db, request), evaluated_by="test"
            )

        first = compare()
        assert_interval_abstention(first)
        assert first.charge_ownership["candidate"].status == "declared"
        assert first.source_evidence["candidate"].version == "stored-assertions/4"
        record_execution(
            execution(version="v2", event_id="summary", cost_role="summary", cost_usd=None),
            db,
            user(),
        )
        second = compare()
        assert second.decision_id != first.decision_id
        assert second.candidate == first.candidate
        assert second.charge_ownership["candidate"].summary_records == 1
        record_execution(execution(version="v2", event_id="legacy", cost_usd="0"), db, user())
        legacy = compare()
        assert legacy.charge_ownership["candidate"].status == "unverified"
        assert legacy.charge_ownership["candidate"].unattributed_records == 1
        assert "source_completeness_unverified" in legacy.limitations
        history = {report.decision_id: report for report in list_retained_decisions(db)}
        assert history[first.decision_id] == first
        assert history[second.decision_id] == second


def test_http_conflicting_charge_does_not_consume_an_event_allowance(engine, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from zeroth.econ.analytics.service_auth import mint_econ_service_token
    from zeroth.econ.plane.cloud.api import router
    from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
    from zeroth.econ.plane.cloud.entitlements import PLAN_CATALOG, PlanLimits
    from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
    from zeroth.econ.plane.config import settings

    now = datetime.now(UTC)
    with Session(engine) as raw:
        raw.add(
            CloudSubscription(
                tenant_id="tenant-a",
                plan="trial",
                status="trialing",
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=13),
                updated_at=now,
            )
        )
        raw.commit()
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield scoped(raw)

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(
            event_limit=2,
            decision_scan_limit=1,
            backtest_limit=1,
            backtest_call_limit=1,
            schedule_limit=1,
            minimum_schedule_interval_minutes=1440,
        ),
    )
    headers = {"Authorization": f"Bearer {mint_econ_service_token()}"}
    with TestClient(app) as client:
        event = charge().model_dump(mode="json")
        assert client.post("/v1/executions", json=event, headers=headers).status_code == 200
        response = client.post(
            "/v1/executions", json={**event, "event_id": "other"}, headers=headers
        )
        assert response.status_code == 422
        assert "charge_id" in response.json()["detail"]
        with Session(engine) as raw:
            assert (
                scoped(raw)
                .scalars(
                    select(CloudUsageCounter.quantity).where(CloudUsageCounter.meter == "events")
                )
                .one()
                == 1
            )
        assert (
            client.post("/v1/executions", json=event, headers=headers).json()["status"]
            == "duplicate"
        )
        response = client.post(
            "/v1/executions", json={**event, "event_id": "new", "charge_id": "new"}, headers=headers
        )
        assert response.status_code == 200
