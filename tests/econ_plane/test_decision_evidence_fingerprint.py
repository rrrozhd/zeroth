"""Retained revisions identify their actual inputs, not just matching totals."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.econ_plane.test_economic_decision_api import _seed_version
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
from zeroth.econ.plane.decisioning.service import (
    _source_fingerprint,
    compare_versions_from_store,
    list_retained_decisions,
    retain_decision,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


@pytest.mark.parametrize("late_evidence", ["equivalent_outcome", "zero_cost_step"])
def test_late_input_with_identical_totals_creates_a_new_retained_revision(tmp_path, late_evidence):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fingerprint.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as raw:
        _seed_version(raw, tenant_id="tenant-a", version="v1", cost="1", accepted=9)
        _seed_version(raw, tenant_id="tenant-a", version="v2", cost="0.6", accepted=9)
        raw.commit()
    request = VersionComparisonRequest(
        workflow="invoice-agent",
        baseline_version="v1",
        candidate_version="v2",
        policy={"min_success_rate": 0.85},
    )

    def compare():
        with Session(engine) as raw:
            db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
            return retain_decision(
                db, request, compare_versions_from_store(db, request), evaluated_by="test"
            )

    first = compare()
    assert set(first.source_evidence) == {"baseline", "candidate"}
    assert first.source_evidence["candidate"].model_dump().keys() == {
        "version",
        "digest",
        "execution_records",
        "outcome_records",
    }
    assert compare().decision_id == first.decision_id
    with Session(engine) as raw:
        timestamp = datetime(2026, 8, 31, tzinfo=UTC) + timedelta(hours=1)
        if late_evidence == "equivalent_outcome":
            raw.add(
                OutcomeEvent(
                    tenant_id="tenant-a",
                    join_key="v2-0",
                    execution_id="",
                    capability_id="invoice-agent",
                    implementation_id="v2",
                    outcome_type="accepted",
                    outcome_payload_json={"accepted": True},
                    outcome_value="true",
                    occurred_at=timestamp,
                    ingested_at=timestamp,
                    outcome_timestamp=timestamp,
                    provenance="MEASURED",
                )
            )
        else:
            raw.add(
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id="v2-0:cleanup:1",
                    join_key="v2-0",
                    timestamp=timestamp,
                    capability_id="invoice-agent",
                    implementation_id="v2",
                    model_version="model-a",
                    token_cost_usd=Decimal("0"),
                    tool_cost_usd=Decimal("0"),
                    compute_cost_usd=Decimal("0"),
                    cost_measurement="measured",
                    usage_measurement="measured",
                    event_metadata={"step": "cleanup"},
                )
            )
        raw.commit()
    second = compare()
    assert first.source_evidence["candidate"].execution_records == 10
    assert first.source_evidence["candidate"].outcome_records == 10
    assert second.source_evidence["baseline"] == first.source_evidence["baseline"]
    assert second.source_evidence["candidate"].digest != first.source_evidence["candidate"].digest
    assert second.source_evidence["candidate"].execution_records == (
        11 if late_evidence == "zero_cost_step" else 10
    )
    assert second.source_evidence["candidate"].outcome_records == (
        11 if late_evidence == "equivalent_outcome" else 10
    )
    assert "source_completeness_unverified" in second.limitations
    assert second.baseline == first.baseline
    assert second.candidate == first.candidate
    assert second.verdict == first.verdict == "pass"
    assert second.decision_id != first.decision_id
    assert compare().decision_id == second.decision_id
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        history = {item.decision_id: item for item in list_retained_decisions(db)}
        assert history[first.decision_id] == first
        assert history[second.decision_id] == second
        assert len(list(db.scalars(select(ExecutionEvent)))) == (
            21 if late_evidence == "zero_cost_step" else 20
        )


def _assertions():
    timestamp = datetime(2026, 9, 6, tzinfo=UTC)
    return ExecutionEvent(
        tenant_id="tenant-a",
        execution_id="event-a",
        join_key="run-a",
        timestamp=timestamp,
        capability_id="workflow",
        implementation_id="v1",
        model_version="model-a",
        token_cost_usd=Decimal("0.10"),
        cost_measurement="measured",
        event_metadata={"provider": "example", "nested": {"a": True, "b": "ok"}},
    ), OutcomeEvent(
        tenant_id="tenant-a",
        join_key="run-a",
        execution_id="run-a",
        capability_id="workflow",
        implementation_id="v1",
        outcome_type="accepted",
        outcome_value="True",
        outcome_payload_json={"accepted": True, "metadata": {"x": 1, "y": 2}},
        occurred_at=timestamp,
        outcome_timestamp=timestamp,
        ingested_at=timestamp,
        provenance="MEASURED",
    )


@pytest.mark.parametrize(
    "nonassertion_change",
    [
        "decimal_scale",
        "receipt_and_row_id",
        "json_key_order",
        "same_instant",
        "assignment_metadata",
    ],
)
def test_fingerprint_normalizes_equivalent_assertions(nonassertion_change):
    from datetime import timezone

    event, outcome = _assertions()
    before = _source_fingerprint("tenant-a", [event], [outcome])
    if nonassertion_change == "decimal_scale":
        event.token_cost_usd = Decimal("0.10000000")
    elif nonassertion_change == "receipt_and_row_id":
        event.id, outcome.id = 901, 902
        outcome.ingested_at += timedelta(days=1)
    elif nonassertion_change == "json_key_order":
        event.event_metadata = {"nested": {"b": "ok", "a": True}, "provider": "example"}
        outcome.outcome_payload_json = {"metadata": {"y": 2, "x": 1}, "accepted": True}
    elif nonassertion_change == "same_instant":
        event.timestamp = event.timestamp.astimezone(timezone(timedelta(hours=-4)))
        outcome.occurred_at = outcome.occurred_at.astimezone(timezone(timedelta(hours=5)))
    else:
        event.event_metadata["experiment_id"] = "derived-assignment"
    assert _source_fingerprint("tenant-a", [event], [outcome]) == before


@pytest.mark.parametrize(
    "assertion_change",
    ["tenant", "model", "provider_request", "typed_metadata", "outcome_time", "outcome_source"],
)
def test_fingerprint_changes_when_selected_assertions_change(assertion_change):
    event, outcome = _assertions()
    before = _source_fingerprint("tenant-a", [event], [outcome])
    tenant = "tenant-a"
    if assertion_change == "tenant":
        tenant = "tenant-b"
    elif assertion_change == "model":
        event.model_version = "model-b"
    elif assertion_change == "provider_request":
        event.provider_request_id = "another-request"
    elif assertion_change == "typed_metadata":
        event.event_metadata["nested"]["a"] = 1  # JSON true is a distinct assertion.
    elif assertion_change == "outcome_time":
        outcome.occurred_at += timedelta(seconds=1)
    else:
        outcome.provenance = "INFERRED"
    assert _source_fingerprint(tenant, [event], [outcome]).digest != before.digest


def test_record_order_is_irrelevant_but_multiplicity_is_preserved():
    first, outcome = _assertions()
    second, _ = _assertions()
    second.execution_id = "event-b"
    before = _source_fingerprint("tenant-a", [first, second], [outcome])
    assert _source_fingerprint("tenant-a", [second, first], [outcome]) == before
    duplicated = _source_fingerprint("tenant-a", [first, second, first], [outcome])
    assert duplicated.execution_records == 3
    assert duplicated.digest != before.digest


def test_historical_reports_do_not_acquire_a_source_binding():
    from zeroth.econ.decisioning import EconomicDecision, VersionEvidence, compare_workflow_versions

    report = compare_workflow_versions(
        VersionEvidence(workflow="workflow", version="v1"),
        VersionEvidence(workflow="workflow", version="v2"),
    ).model_dump(mode="json")
    report.pop("source_evidence")
    report["claim_class"] = "legacy_unclassified"
    report["method_version"] = "legacy_unversioned"
    historical = EconomicDecision.model_validate(report)
    assert historical.source_evidence == {}
    assert historical.claim_class == "legacy_unclassified"
    assert historical.method_version == "legacy_unversioned"
