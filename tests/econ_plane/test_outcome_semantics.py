"""An independent truth table must agree across stored economic readers."""

from datetime import timedelta

import pytest
from sqlalchemy import select
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
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from zeroth.econ.plane.debugger.service import (
    create_outcome_definition,
    resolve_outcomes_for_events,
)
from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
from zeroth.econ.plane.decisioning.service import (
    _version_from_store,
    compare_versions_from_store,
    retain_decision,
    list_retained_decisions,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.instrumentation.schemas import OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_outcome

engine = database_engine


def define(db, version="v1", **changes):
    return create_outcome_definition(
        db,
        OutcomeDefinitionCreate(
            workflow_id="invoice",
            workflow_version=version,
            **{"outcome_type": "approval", "operator": "equals", "target": True, **changes},
        ),
    )


def observe(db, value, version="v1", **changes):
    event = db.scalars(
        select(ExecutionEvent).where(ExecutionEvent.workflow_version == version)
    ).one()
    return ingest_outcome(
        db,
        OutcomeEventCreate(
            capability_id=event.capability_id,
            implementation_id=event.implementation_id,
            execution_id=event.execution_id,
            join_key=event.join_key,
            outcome_type="approval",
            outcome_value=value,
            **{
                "occurred_at": NOW, "provenance": "MEASURED",
                "maturity": "final" if value is not None else "unknown", **changes,
            },
        ),
    )


@pytest.mark.parametrize(
    "operator,target,value,expected",
    [
        ("equals", True, True, True),
        ("equals", False, False, True),
        ("greater_than_or_equal", 7, 8, True),
        ("less_than_or_equal", 7, 8, False),
        ("equals", True, 1, False),
        ("equals", "approved", "approved", True),
        ("equals", True, None, None),
        ("equals", "", "", True),
        (None, None, True, None),
    ],
)
def test_readers_apply_the_same_declared_truth_table(engine, operator, target, value, expected):
    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(execution(event_id="event"), db, user())
        if operator:
            define(db, operator=operator, target=target)
        observe(db, value)
        events = list(db.scalars(select(ExecutionEvent)))
        version = _version_from_store(db, workflow="invoice", version="v1", outcome_type="approval")
        resolved = resolve_outcomes_for_events(db, events)
        assert version.runs[0].accepted is expected
        assert resolved.get(("invoice", "v1", "shared")) is expected


@pytest.mark.parametrize("latest", ["pending", "NaN", "Infinity"])
def test_latest_uninterpretable_outcome_does_not_revive_an_older_success(engine, latest):
    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(execution(event_id="event"), db, user())
        define(db, operator="greater_than_or_equal", target=7)
        observe(db, 8)
        observe(db, latest, occurred_at=NOW + timedelta(hours=1))
        events = list(db.scalars(select(ExecutionEvent)))
        assert resolve_outcomes_for_events(db, events).get(("invoice", "v1", "shared")) is None
        result = _version_from_store(db, workflow="invoice", version="v1", outcome_type="approval")
        assert result.runs[0].accepted is None


@pytest.mark.parametrize("definition_case", ["both", "missing", "different_rule", "wrong_type"])
def test_comparisons_require_compatible_definitions(engine, definition_case):
    request = VersionComparisonRequest(
        workflow="invoice",
        baseline_version="v1",
        candidate_version="v2",
        outcome_type="approval",
        policy={"min_runs": 1, "min_success_rate": 0.5},
    )
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            record_execution(execution(version=version, event_id=version), db, user())
            observe(db, True, version)
            if version == "v2" and definition_case == "missing":
                continue
            define(
                db,
                version,
                **(
                    {"operator": "not_equals", "target": False}
                    if version == "v2" and definition_case == "different_rule"
                    else {"outcome_type": "fraud_flag"}
                    if version == "v2" and definition_case == "wrong_type"
                    else {}
                ),
            )
        report = compare_versions_from_store(db, request)
        assert report.verdict == ("pass" if definition_case == "both" else "abstain")
        assert report.method_version == "observed-policy/3"
        if definition_case == "different_rule":
            assert "outcome_semantics_incompatible" in report.reason_codes
        if definition_case in {"missing", "wrong_type"}:
            assert report.candidate.labeled_runs == 0
            assert report.candidate.cost_per_accepted_outcome_usd is None


def test_definition_arrival_creates_a_new_bound_revision_and_keeps_history(engine):
    request = VersionComparisonRequest(
        workflow="invoice",
        baseline_version="v1",
        candidate_version="v2",
        outcome_type="approval",
        policy={"min_runs": 1, "min_success_rate": 0.5},
    )
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            record_execution(execution(version=version, event_id=version), db, user())
            record_outcome(outcome(version=version, outcome_type="approval"), db, user())

        def compare():
            return retain_decision(
                db, request, compare_versions_from_store(db, request), evaluated_by="test"
            )

        first = compare()
        assert first.verdict == "abstain"
        for version in ("v1", "v2"):
            define(db, version)
        second = compare()
        assert second.verdict == "pass"
        assert second.decision_id != first.decision_id
        assert second.source_evidence == first.source_evidence
        assert (
            second.outcome_semantics["baseline"].rule_digest
            == second.outcome_semantics["candidate"].rule_digest
        )
        assert (
            second.outcome_semantics["baseline"].definition_digest
            != second.outcome_semantics["candidate"].definition_digest
        )
        history = {report.decision_id: report for report in list_retained_decisions(db)}
        assert history[first.decision_id] == first
        assert history[second.decision_id] == second


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_definition_creation_preserves_one_immutable_rule(engine, monkeypatch, conflict):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from zeroth.econ.plane.scoped_session import ScopedSession
    from zeroth.econ.plane.debugger.models import OutcomeDefinition

    barrier = Barrier(2)
    commit = ScopedSession.commit

    def concurrent_commit(db):
        barrier.wait(timeout=10)
        return commit(db)

    monkeypatch.setattr(ScopedSession, "commit", concurrent_commit)

    def create(target):
        with Session(engine) as raw:
            try:
                inserted, _ = define(scoped(raw), target=target)
                return "inserted" if inserted else "duplicate"
            except ValueError as error:
                assert "immutable" in str(error)
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, target) for target in (True, not conflict)]
        assert sorted(future.result(timeout=20) for future in futures) == (
            ["conflict", "inserted"] if conflict else ["duplicate", "inserted"]
        )
    with Session(engine) as raw:
        assert len(list(scoped(raw).scalars(select(OutcomeDefinition)))) == 1


def test_cloud_definition_routes_preserve_admin_and_tenant_boundaries(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate
    from zeroth.econ.plane.cloud.keys_service import issue_api_key
    from zeroth.econ.plane.database import get_db
    from zeroth.econ.plane.instrumentation.api import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def raw_db():
        with Session(engine) as raw:
            yield raw

    app.dependency_overrides[get_db] = raw_db
    keys = {}
    for tenant, role in [("tenant-a", "Admin"), ("tenant-a", "Viewer"), ("tenant-b", "Admin")]:
        with Session(engine) as raw:
            reveal = issue_api_key(
                scoped(raw, tenant),
                ApiKeyCreate(name="test", roles=[role]),
                subject="test-owner",
                workspace_id=None,
            )
            keys[tenant, role] = {"Authorization": f"Bearer {reveal.api_key}"}
    payload = OutcomeDefinitionCreate(
        workflow_id="invoice",
        workflow_version="v1",
        outcome_type="approval",
        operator="equals",
        target=True,
    ).model_dump()
    path = "/v1/debugger/outcome-definitions"
    with TestClient(app) as client:
        assert client.post(path, json=payload).status_code == 401
        assert (
            client.post(path, json=payload, headers=keys["tenant-a", "Viewer"]).status_code == 403
        )
        first = client.post(path, json=payload, headers=keys["tenant-a", "Admin"])
        assert first.status_code == 201
        assert client.post(path, json=payload, headers=keys["tenant-a", "Admin"]).status_code == 200
        assert (
            client.post(
                path, json={**payload, "target": False}, headers=keys["tenant-a", "Admin"]
            ).status_code
            == 409
        )
        assert len(client.get(path, headers=keys["tenant-a", "Viewer"]).json()) == 1
        assert client.get(path, headers=keys["tenant-b", "Admin"]).json() == []
        assert (
            client.post(
                path, json={**payload, "tenant_id": "tenant-b"}, headers=keys["tenant-a", "Admin"]
            ).status_code
            == 422
        )
        assert (
            client.post(
                path, json={**payload, "target": False}, headers=keys["tenant-b", "Admin"]
            ).status_code
            == 201
        )
