"""SDK namespace contract across ingestion, retained history and report readers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import product
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from tests.conftest import requires_docker

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.cloud.api import record_execution, record_outcome
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent, SdkOutcomeEvent
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from zeroth.econ.plane.debugger.service import (
    breakage,
    cohorts,
    create_outcome_definition,
    diagnostic_report,
    timeline,
)
from zeroth.econ.plane.decisioning.service import _version_from_store
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.instrumentation.service import ingest_execution, ingest_outcome
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.reconciliation import models as reconciliation_models  # noqa: F401
from zeroth.platform.storage.scoping import TenantWideScopeContext

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=[pytest.mark.postgres, requires_docker])]
)
def engine(request, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_register_ingest_capabilities", True)
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    admin = None
    if request.param == "postgres":
        container = request.getfixturevalue("postgres_container")
        root = make_url(container.get_connection_url().replace("psycopg2", "psycopg"))
        name = f"econ_namespace_{uuid4().hex[:10]}"
        admin = create_engine(root.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        engine = create_engine(root.set(database=name))
    else:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'namespace.db'}")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}"'))
            admin.dispose()


def scoped(raw, tenant="tenant-a"):
    return ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant))


def user(tenant="tenant-a"):
    return ScopedUserClaims(
        sub="test-owner",
        email="owner@example.com",
        roles=["Admin"],
        tenant_id=tenant,
        exp=2000000000,
        iss="test",
    )


def execution(workflow="invoice", version="v1", run="shared", **changes):
    return SdkExecutionEvent.model_validate(
        {
            "workflow": workflow,
            "workflow_version": version,
            "run_id": run,
            "step": "extract",
            "recorded_at": NOW,
            "cost_usd": "0.10",
            "subject_id": "customer",
            **changes,
        }
    )


def outcome(workflow="invoice", version="v1", run="shared", **changes):
    return SdkOutcomeEvent.model_validate(
        {
            "workflow": workflow,
            "workflow_version": version,
            "run_id": run,
            "accepted": True,
            "occurred_at": NOW,
            **changes,
        }
    )


def definition(db, workflow, version):
    create_outcome_definition(
        db,
        OutcomeDefinitionCreate(
            workflow_id=workflow,
            workflow_version=version,
            outcome_type="accepted",
            operator="equals",
            target=True,
        ),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_names_remain_independent_across_tenants_workflows_versions(engine, reverse):
    cases = list(product(["tenant-a", "tenant-b"], ["invoice", "email"], ["v1", "v2"]))
    if reverse:
        cases.reverse()
    for tenant, workflow, version in cases:
        with Session(engine) as raw:
            db = scoped(raw, tenant)
            who = user(tenant)
            event = execution(workflow, version)
            result = outcome(workflow, version, accepted=version == "v1")
            assert record_execution(event, db, who).status == "inserted"
            assert record_outcome(result, db, who).status == "inserted"
            assert record_execution(event, db, who).status == "duplicate"
            assert record_outcome(result, db, who).status == "duplicate"
            definition(db, workflow, version)
    with Session(engine) as raw:
        executions = list(raw.scalars(select(ExecutionEvent)))
        outcomes = list(raw.scalars(select(OutcomeEvent)))
        assert len(executions) == len(outcomes) == 8
        assert len({event.capability_id for event in executions}) == 4
        assert len({event.implementation_id for event in executions}) == 8
    for tenant, workflow, version in cases:
        with Session(engine) as raw:
            db = scoped(raw, tenant)
            evidence = _version_from_store(
                db, workflow=workflow, version=version, outcome_type="accepted"
            )
            assert len(evidence.runs) == 1
            assert evidence.runs[0].cost_usd == Decimal("0.10")
            assert evidence.runs[0].accepted is (version == "v1")
            points = {point.workflow_version: point for point in timeline(db, workflow_id=workflow)}
            assert points["v1"].successful_runs == 1
            assert points["v1"].failed_runs == 0
            assert points["v2"].successful_runs == 0
            assert points["v2"].failed_runs == 1
            report = diagnostic_report(db, workflow_id=workflow)
            assert (report.runs, report.successful_runs, report.failed_runs) == (2, 1, 1)
            assert report.measured_failure_exposure_usd == 0.1
            cohort = cohorts(db, workflow_id=workflow, group_by="subject_id")[0]
            assert (cohort.runs, cohort.successful_runs, cohort.failed_runs) == (2, 1, 1)
            failed = breakage(db, workflow_id=workflow)
            assert [(point.workflow_version, point.failed_runs) for point in failed] == [("v2", 1)]


@pytest.mark.parametrize(
    "names",
    [
        [("a:b", "c"), ("a", "b:c")],
        [("a/b", "c"), ("a", "b/c")],
        [("é", "v1"), ("e\u0301", "v1")],
        [("w" * 128, "v" * 128), ("w" * 127 + "x", "v" * 128)],
    ],
)
def test_namespace_does_not_conflate_delimiters_unicode_or_long_names(engine, names):
    with Session(engine) as raw:
        db = scoped(raw)
        for workflow, version in names:
            record_execution(execution(workflow, version), db, user())
            record_outcome(outcome(workflow, version), db, user())
        rows = list(db.scalars(select(ExecutionEvent)))
        assert len({row.implementation_id for row in rows}) == len(names)
        assert all(len(row.implementation_id) <= 128 for row in rows)
        assert {(row.workflow_id, row.workflow_version) for row in rows} == set(names)


def test_legacy_sdk_history_and_retries_keep_original_identity(engine):
    # Actual pre-namespace adapter shape, including value inserted by lower ingestion.
    from zeroth.econ.plane.cloud.api import _CloudOutcomeCreate, _stable_execution_id

    with Session(engine) as raw:
        db = scoped(raw)
        event = execution(subject_id=None)
        ingest_execution(
            db,
            ExecutionEventCreate(
                tenant_id="tenant-a",
                execution_id=_stable_execution_id(event),
                join_key="shared",
                timestamp=NOW,
                capability_id="invoice",
                implementation_id="v1",
                model_version=event.model_version,
                token_cost_usd=Decimal("0.10"),
                tool_cost_usd=None,
                compute_cost_usd=None,
                cost_measurement="measured",
                usage_measurement="unmeasured",
                latency_ms=event.latency_ms,
                metadata={"step": "extract", "attempt": 1, "tenant_id": "tenant-a"},
            ),
        )
        ingest_outcome(
            db,
            _CloudOutcomeCreate(
                tenant_id="tenant-a",
                join_key="shared",
                capability_id="invoice",
                implementation_id="v1",
                outcome_type="accepted",
                outcome_value=True,
                outcome_payload_json={"accepted": True, "metadata": {}},
                occurred_at=NOW,
                outcome_timestamp=NOW,
            ),
        )
        assert record_execution(event, db, user()).status == "duplicate"
        assert record_outcome(outcome(), db, user()).status == "duplicate"
        record_execution(execution(run="new", subject_id=None), db, user())
        record_outcome(outcome(run="new"), db, user())
        record_execution(execution(version="v2"), db, user())
        record_outcome(outcome(version="v2"), db, user())
        assert len(list(db.scalars(select(ExecutionEvent)))) == 3
        for version, expected in [("v1", 2), ("v2", 1)]:
            evidence = _version_from_store(
                db, workflow="invoice", version=version, outcome_type="accepted"
            )
            assert len(evidence.runs) == expected
            assert all(run.accepted is True for run in evidence.runs)
        first = db.scalars(select(ExecutionEvent).where(ExecutionEvent.join_key == "new")).one()
        assert (first.capability_id, first.implementation_id) == ("invoice", "v1")


def test_legacy_debugger_outcomes_use_execution_identity_not_bare_run_id(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        for index, (workflow, version, accepted) in enumerate(
            [
                ("invoice", "v1", True),
                ("invoice", "v2", False),
                ("email", "v1", False),
            ]
        ):
            cap, impl = f"stored:{workflow}", f"stored:{workflow}:{version}"
            ingest_execution(
                db,
                ExecutionEventCreate(
                    execution_id=f"event-{index}",
                    join_key="shared",
                    timestamp=NOW,
                    capability_id=cap,
                    implementation_id=impl,
                    model_version="test",
                    workflow_id=workflow,
                    workflow_version=version,
                    run_id="shared",
                    step_id="extract",
                    token_cost_usd=Decimal("0.10"),
                    cost_measurement="measured",
                ),
            )
            ingest_outcome(
                db,
                OutcomeEventCreate(
                    join_key="shared",
                    capability_id=cap,
                    implementation_id=impl,
                    outcome_type="approval",
                    outcome_value=accepted,
                    occurred_at=NOW + timedelta(seconds=index),
                ),
            )
            create_outcome_definition(
                db,
                OutcomeDefinitionCreate(
                    workflow_id=workflow,
                    workflow_version=version,
                    outcome_type="approval",
                    operator="equals",
                    target=True,
                ),
            )
        report = diagnostic_report(db, workflow_id="invoice")
        assert (report.runs, report.successful_runs, report.failed_runs) == (2, 1, 1)
        assert report.measured_failure_exposure_usd == 0.1
        evidence = _version_from_store(
            db, workflow="invoice", version="v1", outcome_type="approval"
        )
        assert len(evidence.runs) == 1
        assert evidence.runs[0].accepted is True


@pytest.mark.parametrize("existing_capability", [False, True])
def test_concurrent_first_registration_accepts_the_owned_winner(engine, existing_capability):
    from tests.econ_plane.test_outcome_ingest_race import _held_sessionmaker

    if existing_capability:
        with Session(engine) as raw:
            record_execution(execution(version="v0"), scoped(raw), user())
    winners = []

    def commit_winner():
        with Session(engine) as raw:
            winners.append(record_execution(execution(run="winner"), scoped(raw), user()).status)

    collisions = []
    factory = _held_sessionmaker(engine, commit_winner, lost=collisions)
    with factory() as raw:
        assert record_execution(execution(run="loser"), scoped(raw), user()).status == "inserted"
    assert winners == ["inserted"]
    assert len(collisions) == 1  # Demonstrate constraint recovery, not sequential replay.
    with Session(engine) as raw:
        evidence = _version_from_store(
            scoped(raw), workflow="invoice", version="v1", outcome_type="accepted"
        )
        assert {run.run_id for run in evidence.runs} == {"winner", "loser"}


def test_outcome_before_registration_is_visible_and_retryable(engine):
    from fastapi import HTTPException

    with Session(engine) as raw:
        db = scoped(raw)
        with pytest.raises(HTTPException) as failure:
            record_outcome(outcome(), db, user())
        assert failure.value.status_code == 422
        assert list(db.scalars(select(OutcomeEvent))) == []
        record_execution(execution(), db, user())
        assert record_outcome(outcome(), db, user()).status == "inserted"


def test_explicit_execution_id_cannot_move_between_public_identities(engine):
    from fastapi import HTTPException

    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(execution(event_id="caller-id"), db, user())
        with pytest.raises(HTTPException) as failure:
            record_execution(execution(workflow="email", event_id="caller-id"), db, user())
        assert failure.value.status_code == 422
        assert len(list(db.scalars(select(ExecutionEvent)))) == 1


def test_legacy_writer_cannot_switch_an_existing_sdk_mapping(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        event = execution()
        record_execution(event, db, user())
        ingest_execution(
            db,
            ExecutionEventCreate(
                execution_id="legacy-later",
                join_key="other",
                timestamp=NOW,
                capability_id="invoice",
                implementation_id="v1",
                model_version="test",
            ),
        )
        assert record_execution(event, db, user()).status == "duplicate"


def test_ambiguous_legacy_storage_identity_remains_unresolved_in_a_single_version_read(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            ingest_execution(
                db,
                ExecutionEventCreate(
                    execution_id=f"event-{version}",
                    join_key="shared",
                    timestamp=NOW,
                    capability_id="invoice",
                    implementation_id="shared-model",
                    model_version="test",
                    workflow_id="invoice",
                    workflow_version=version,
                    run_id="shared",
                ),
            )
        ingest_outcome(
            db,
            OutcomeEventCreate(
                join_key="shared",
                capability_id="invoice",
                implementation_id="shared-model",
                outcome_type="approval",
                outcome_value=True,
                occurred_at=NOW,
            ),
        )
        for version in ("v1", "v2"):
            evidence = _version_from_store(
                db, workflow="invoice", version=version, outcome_type="approval"
            )
            assert len(evidence.runs) == 1
            assert evidence.runs[0].accepted is None


def test_provider_allocation_uses_workflow_version_run_identity(engine):
    from zeroth.econ.plane.reconciliation.schemas import ProviderBillImportRequest
    from zeroth.econ.plane.reconciliation.service import import_provider_bill, provider_bill_report

    with Session(engine) as raw:
        db = scoped(raw)
        for workflow, version, accepted in [
            ("invoice", "v1", True),
            ("invoice", "v2", False),
            ("email", "v1", False),
        ]:
            record_execution(
                execution(workflow, version, metadata={"provider": "openai"}), db, user()
            )
            record_outcome(outcome(workflow, version, accepted=accepted), db, user())
            definition(db, workflow, version)
        import_provider_bill(
            db,
            ProviderBillImportRequest(
                statement_id="statement",
                provider="openai",
                period_start=NOW,
                period_end=NOW + timedelta(days=1),
                billed_total_usd="3.00",
                source_kind="manual",
                buckets=[
                    {
                        "bucket_id": "all",
                        "period_start": NOW,
                        "period_end": NOW + timedelta(days=1),
                        "amount_usd": "3.00",
                    }
                ],
            ),
        )
        report = provider_bill_report(db, provider="openai", statement_id="statement")
        assert {
            (row.workflow_id, row.workflow_version, row.outcome_status, row.billed_cost_usd)
            for row in report.allocations
        } == {
            ("invoice", "v1", "success", Decimal("1.00")),
            ("invoice", "v2", "failure", Decimal("1.00")),
            ("email", "v1", "failure", Decimal("1.00")),
        }


def test_outcome_schema_upgrade_preserves_legacy_identity_on_both_databases(engine):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from zeroth.econ.plane.database import _missing_chain_owned_columns

    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_18_outcome_workflow_identity"
    )
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert {item[1] for item in _missing_chain_owned_columns(conn)} == {
            "workflow_id",
            "workflow_version",
        }
        conn.execute(
            text(
                "INSERT INTO outcome_events (tenant_id, join_key, execution_id, capability_id, "
                "implementation_id, outcome_type, outcome_payload_json, outcome_value, "
                "occurred_at, ingested_at, outcome_timestamp, provenance) VALUES "
                "('tenant-a', 'shared', 'shared', 'invoice', 'v1', 'accepted', '{}', 'True', "
                "'2026-09-06', '2026-09-06', '2026-09-06', 'MEASURED')"
            )
        )
        migration.upgrade()
        assert _missing_chain_owned_columns(conn) == ()
        assert conn.execute(
            text(
                "SELECT capability_id, implementation_id, workflow_id, workflow_version "
                "FROM outcome_events"
            )
        ).one() == ("invoice", "v1", None, None)


def test_existing_tenant_erasure_covers_scoped_sdk_ids_without_cross_tenant_deletion(engine):
    from sqlalchemy.orm import sessionmaker
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser

    for tenant, workflow in product(["tenant-a", "tenant-b"], ["invoice", "email"]):
        with Session(engine) as raw:
            db = scoped(raw, tenant)
            record_execution(execution(workflow), db, user(tenant))
            record_outcome(outcome(workflow), db, user(tenant))
    eraser = SqlAlchemyEconEventEraser(sessionmaker(bind=engine))
    assert eraser._delete_sync("tenant-a", ["shared"], "erase-a") == 4
    assert eraser._delete_sync("tenant-a", ["shared"], "erase-a") == 4
    with Session(engine) as raw:
        for model in (ExecutionEvent, OutcomeEvent):
            remaining = list(raw.scalars(select(model)))
            assert len(remaining) == 2
            assert {row.tenant_id for row in remaining} == {"tenant-b"}


def test_unrelated_identity_combinations_cannot_consume_the_outcome_read_limit(engine):
    from zeroth.econ.plane.instrumentation.identity import outcomes_for_events

    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(execution("invoice", run="run-a"), db, user())
        record_execution(execution("email", run="run-b"), db, user())
        record_outcome(outcome("invoice", run="run-a"), db, user())
        record_outcome(
            outcome("invoice", run="run-b", occurred_at=NOW + timedelta(seconds=1)), db, user()
        )
        events = list(db.scalars(select(ExecutionEvent)))
        resolved = outcomes_for_events(db, events, limit=1)
        assert [(key, row.join_key) for key, row in resolved] == [
            (("invoice", "v1", "run-a"), "run-a")
        ]


def test_identity_query_handles_the_existing_debugger_event_bound(engine):
    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError
    from zeroth.econ.plane.debugger.service import MAX_DEBUGGER_EVENTS
    from zeroth.econ.plane.instrumentation.identity import outcomes_for_events

    count = MAX_DEBUGGER_EVENTS
    # Distinct workflows exercise the advertised event bound without relying
    # on every caller sharing a tiny workflow/version catalog.
    with engine.begin() as conn:
        conn.execute(
            insert(ExecutionEvent),
            [
                {
                    "tenant_id": "tenant-a",
                    "execution_id": f"event-{index}",
                    "join_key": f"run-{index}",
                    "timestamp": NOW,
                    "capability_id": f"stored-{index}",
                    "implementation_id": f"impl-{index}",
                    "workflow_id": f"workflow-{index}",
                    "workflow_version": "v1",
                    "run_id": f"run-{index}",
                    "model_version": "test",
                }
                for index in range(count)
            ],
        )
        conn.execute(
            insert(OutcomeEvent),
            {
                "tenant_id": "tenant-a",
                "join_key": "run-0",
                "execution_id": "run-0",
                "capability_id": "stored-0",
                "implementation_id": "impl-0",
                "workflow_id": "workflow-0",
                "workflow_version": "v1",
                "outcome_type": "accepted",
                "outcome_payload_json": {"accepted": True},
                "outcome_value": "True",
                "occurred_at": NOW,
                "outcome_timestamp": NOW,
                "ingested_at": NOW,
            },
        )
    with Session(engine) as raw:
        db = scoped(raw)
        events = list(db.scalars(select(ExecutionEvent)))
        try:
            resolved = outcomes_for_events(db, events, limit=MAX_DEBUGGER_EVENTS)
        except DBAPIError as exc:
            pytest.fail(
                f"Identity query failed at the existing {count}-event bound: {type(exc.orig).__name__}",
                pytrace=False,
            )
        assert [key for key, _ in resolved] == [("workflow-0", "v1", "run-0")]
