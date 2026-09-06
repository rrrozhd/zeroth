"""Independent producer inventory must expose loss before economic policy runs."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from fastapi import HTTPException
import pytest
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW,
    engine as database_engine,
    execution,
    outcome,
    scoped,
    user,
)
from zeroth.econ.plane.cloud.api import record_execution, record_outcome
from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
from zeroth.econ.plane.decisioning.service import (
    compare_versions_from_store,
    list_retained_decisions,
    retain_decision,
)


engine = database_engine


@pytest.mark.parametrize("window", [None, "batch"])
def test_window_binding_preserves_legacy_debugger_run_mapping(engine, window):
    from sqlalchemy import select
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent

    with Session(engine) as raw:
        db = scoped(raw)
        event = execution(
            run="declared-run",
            event_id="event",
            source_window_id=window,
            metadata={"run_id": "legacy-debugger-run"},
        )
        record_execution(event, db, user())
        stored = db.scalars(select(ExecutionEvent)).one()
        assert stored.run_id == ("declared-run" if window else "legacy-debugger-run")
        assert record_execution(event, db, user()).status == "duplicate"


def reference_digest(ids):
    encoded = json.dumps(
        sorted(ids, key=lambda x: x.encode()), ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def inventory(version, runs=2):
    # Producer ledger is constructed before any delivery attempt or acknowledgement.
    return {
        "source_window_id": "batch",
        "opened_at": NOW.isoformat(),
        "closed_at": (NOW + timedelta(hours=1)).isoformat(),
        "runs": [
            {
                "run_id": f"run-{i}",
                "terminal_state": "completed",
                "execution_count": 1,
                "execution_ids_digest": reference_digest([f"{version}-{i}"]),
            }
            for i in range(runs)
        ],
    }


def request_payload():
    return {
        "workflow": "invoice",
        "baseline_version": "v1",
        "candidate_version": "v2",
        "policy": {"min_runs": 1, "min_success_rate": 0.5},
        "source_windows": {"baseline": inventory("v1"), "candidate": inventory("v2")},
    }


def deliver(db, version, i, **changes):
    event = execution(
        "invoice",
        version,
        f"run-{i}",
        event_id=f"{version}-{i}",
        source_window_id="batch",
        **changes,
    )
    record_execution(event, db, user())
    record_outcome(outcome("invoice", version, f"run-{i}"), db, user())
    return event


@pytest.mark.parametrize("fault", ["none", "missing_run", "wrong_id", "extra_run", "late_time"])
def test_reconciliation_exposes_source_delivery_faults(engine, fault):
    payload = request_payload()
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            for i in range(2):
                if version == "v2" and i == 1 and fault == "missing_run":
                    continue
                if version == "v2" and i == 1 and fault == "wrong_id":
                    record_execution(
                        execution(
                            version=version,
                            run=f"run-{i}",
                            event_id="replacement",
                            source_window_id="batch",
                        ),
                        db,
                        user(),
                    )
                    record_outcome(outcome(version=version, run=f"run-{i}"), db, user())
                else:
                    deliver(
                        db,
                        version,
                        i,
                        **(
                            {"recorded_at": NOW + timedelta(hours=2)}
                            if version == "v2" and i == 1 and fault == "late_time"
                            else {}
                        ),
                    )
        if fault == "extra_run":
            deliver(db, "v2", 2)
        request = VersionComparisonRequest.model_validate(payload)
        report = compare_versions_from_store(db, request)
        result = report.source_delivery["candidate"]
        assert report.source_delivery["baseline"].status == "matched"
        assert result.status == ("matched" if fault == "none" else "mismatch")
        assert report.verdict == ("pass" if fault == "none" else "abstain")
        assert result.expected_runs == 2
        assert result.expected_executions == 2
        assert report.candidate.runs == (3 if fault == "extra_run" else 2)
        assert result.missing_runs == (1 if fault == "missing_run" else 0)
        assert result.unexpected_runs == (1 if fault == "extra_run" else 0)
        assert result.mismatched_runs == (1 if fault in {"missing_run", "wrong_id"} else 0)
        assert result.out_of_window_executions == (1 if fault == "late_time" else 0)
        assert "source_completeness_unverified" in report.limitations
        if fault == "missing_run":
            assert report.candidate.unmeasured_runs == 1
        if fault != "none":
            assert "candidate_source_delivery_mismatch" in report.reason_codes


def test_delivery_after_report_and_inventory_changes_create_immutable_revisions(engine):
    payload = request_payload()
    with Session(engine) as raw:
        db = scoped(raw)
        request = VersionComparisonRequest.model_validate(payload)

        def compare(req=request):
            return retain_decision(
                db, req, compare_versions_from_store(db, req), evaluated_by="test"
            )

        before = compare()  # Whole stream absent, including every run.
        assert before.candidate.runs == before.candidate.unmeasured_runs == 2
        assert before.source_delivery["candidate"].missing_runs == 2
        for version in ("v1", "v2"):
            for i in range(2):
                event = deliver(db, version, i)
                assert record_execution(event, db, user()).status == "duplicate"
        complete = compare()
        assert complete.verdict == "pass"
        assert complete.decision_id != before.decision_id
        assert compare().decision_id == complete.decision_id
        reversed_payload = deepcopy(payload)
        reversed_payload["source_windows"]["candidate"]["runs"].reverse()
        assert (
            compare(VersionComparisonRequest.model_validate(reversed_payload)).decision_id
            == complete.decision_id
        )
        changed = deepcopy(payload)
        changed["source_windows"]["candidate"]["runs"][0]["terminal_state"] = "failed"
        revised = compare(VersionComparisonRequest.model_validate(changed))
        assert revised.candidate == complete.candidate
        assert revised.decision_id != complete.decision_id
        assert (
            revised.source_delivery["candidate"].inventory_digest
            != complete.source_delivery["candidate"].inventory_digest
        )
        deliver(db, "v2", 2)
        late = compare()
        assert late.verdict == "abstain"
        history = {r.decision_id: r for r in list_retained_decisions(db)}
        assert history[complete.decision_id] == complete
        assert history[before.decision_id] == before


def test_zero_record_inventory_run_has_unknown_cost(engine):
    payload = request_payload()
    for manifest in payload["source_windows"].values():
        manifest["runs"] = [
            {
                "run_id": "empty",
                "terminal_state": "cancelled",
                "execution_count": 0,
                "execution_ids_digest": reference_digest([]),
            }
        ]
    with Session(engine) as raw:
        report = compare_versions_from_store(
            scoped(raw), VersionComparisonRequest.model_validate(payload)
        )
    assert report.verdict == "abstain"
    assert report.candidate.runs == report.candidate.unmeasured_runs == 1
    assert report.candidate.cost_per_accepted_outcome_usd is None
    assert report.source_delivery["candidate"].status == "matched"


def test_window_membership_is_immutable_and_scoped(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            for i in range(2):
                event = deliver(db, version, i)
        with pytest.raises(HTTPException) as error:
            record_execution(event.model_copy(update={"source_window_id": "changed"}), db, user())
        assert error.value.status_code == 422
        for tenant, workflow, version, window in [
            ("tenant-b", "invoice", "v2", "batch"),
            ("tenant-a", "email", "v2", "batch"),
            ("tenant-a", "invoice", "v3", "batch"),
            ("tenant-a", "invoice", "v2", "other"),
        ]:
            with Session(engine) as other_raw:
                other = scoped(other_raw, tenant)
                record_execution(
                    execution(
                        workflow,
                        version,
                        "noise",
                        event_id=f"{tenant}-{workflow}-{version}-{window}",
                        source_window_id=window,
                    ),
                    other,
                    user(tenant),
                )
        report = compare_versions_from_store(
            db, VersionComparisonRequest.model_validate(request_payload())
        )
        assert report.verdict == "pass"
        assert report.source_delivery["candidate"].observed_executions == 2
        legacy = request_payload()
        del legacy["source_windows"]
        assert (
            compare_versions_from_store(
                db, VersionComparisonRequest.model_validate(legacy)
            ).source_delivery
            == {}
        )


def test_missing_step_cannot_report_partial_run_cost_as_complete(engine):
    payload = request_payload()
    missing = payload["source_windows"]["candidate"]["runs"][0]
    missing["execution_count"] = 2
    missing["execution_ids_digest"] = reference_digest(["v2-0", "v2-0-second"])
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            for i in range(2):
                deliver(db, version, i)
        request = VersionComparisonRequest.model_validate(payload)
        report = compare_versions_from_store(db, request)
        assert report.verdict == "abstain"
        assert report.candidate.unmeasured_runs == 1
        assert report.candidate.cost_per_accepted_outcome_usd is None
        assert report.source_delivery["candidate"].mismatched_runs == 1
        record_execution(
            execution(
                version="v2",
                run="run-0",
                event_id="v2-0-second",
                step="second",
                cost_usd="0",
                source_window_id="batch",
            ),
            db,
            user(),
        )
        complete = compare_versions_from_store(db, request)
        assert complete.source_delivery["candidate"].status == "matched"
        assert complete.candidate.unmeasured_runs == 0
        assert complete.verdict == "pass"


def test_full_window_bound_and_overflow_abstention(engine):
    from sqlalchemy import insert
    from zeroth.econ.plane.instrumentation.models import ExecutionEvent

    ids = [f"step-{i}" for i in range(50_000)]
    payload = request_payload()
    payload["candidate_version"] = "v1"
    manifest = inventory("v1")
    manifest["runs"] = [
        {
            "run_id": "large",
            "terminal_state": "completed",
            "execution_count": len(ids),
            "execution_ids_digest": reference_digest(ids),
        }
    ]
    payload["source_windows"] = {"baseline": manifest, "candidate": manifest}
    row = dict(
        tenant_id="tenant-a",
        workflow_id="invoice",
        workflow_version="v1",
        capability_id="invoice",
        implementation_id="v1",
        source_window_id="batch",
        join_key="large",
        run_id="large",
        timestamp=NOW,
        model_version="model",
        token_cost_usd=1,
        cost_measurement="measured",
    )
    with engine.begin() as conn:
        conn.execute(insert(ExecutionEvent), [dict(row, execution_id=event_id) for event_id in ids])
    request = VersionComparisonRequest.model_validate(payload)
    with Session(engine) as raw:
        report = compare_versions_from_store(scoped(raw), request)
        assert report.source_delivery["candidate"].status == "matched"
        assert report.source_delivery["candidate"].observed_executions == 50_000
        assert not report.source_delivery["candidate"].scan_truncated
    with engine.begin() as conn:
        conn.execute(insert(ExecutionEvent), dict(row, execution_id="overflow"))
    with Session(engine) as raw:
        overflow = compare_versions_from_store(scoped(raw), request)
        assert overflow.verdict == "abstain"
        assert overflow.source_delivery["candidate"].status == "mismatch"
        assert overflow.source_delivery["candidate"].observed_executions == 50_001
        assert overflow.source_delivery["candidate"].scan_truncated
        assert overflow.candidate.unmeasured_runs == 1


def test_http_window_ingestion_comparison_and_history(engine, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from zeroth.econ.analytics.service_auth import mint_econ_service_token
    from zeroth.econ.plane.cloud.api import router as cloud_router
    from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
    from zeroth.econ.plane.config import settings
    from zeroth.econ.plane.decisioning.api import router as decision_router

    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")
    app.include_router(decision_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield scoped(raw)

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    headers = {"Authorization": f"Bearer {mint_econ_service_token()}"}
    with TestClient(app) as client:
        payload = request_payload()
        for version in ("v1", "v2"):
            for i in range(2):
                event = execution(
                    version=version,
                    run=f"run-{i}",
                    event_id=f"{version}-{i}",
                    source_window_id="batch",
                ).model_dump(mode="json")
                assert client.post("/v1/executions", json=event, headers=headers).status_code == 200
                response = client.post(
                    "/v1/outcomes",
                    headers=headers,
                    json=outcome(version=version, run=f"run-{i}").model_dump(mode="json"),
                )
                assert response.status_code == 200
        response = client.post("/v1/decisions/compare", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["verdict"] == "pass"
        assert report["source_delivery"]["candidate"]["status"] == "matched"
        assert report["source_evidence"]["candidate"]["version"] == "stored-assertions/2"
        history = client.get("/v1/decisions", headers=headers).json()
        assert history == [report]
        assert "runs" not in report["source_delivery"]["candidate"]
        payload["source_windows"].pop("candidate")
        assert (
            client.post("/v1/decisions/compare", json=payload, headers=headers).status_code == 422
        )
