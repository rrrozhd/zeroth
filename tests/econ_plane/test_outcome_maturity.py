"""Declared business maturity is distinct from observed value and provenance."""

from tests.econ.assertions import assert_interval_abstention

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW,
    definition,
    engine as database_engine,
    execution,
    outcome,
    scoped,
    user,
)
from zeroth.econ.plane.cloud.api import record_execution, record_outcome
from zeroth.econ.plane.debugger.service import resolve_outcomes_for_events
from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
from zeroth.econ.plane.decisioning.service import (
    _version_from_store,
    compare_versions_from_store,
    retain_decision,
    list_retained_decisions,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.api import _outcome_out

engine = database_engine


def seed(db, version="v1"):
    definition(db, "invoice", version)
    record_execution(execution(version=version, event_id=version), db, user())


def emit(db, maturity="final", accepted=True, when=NOW, version="v1", **changes):
    event = outcome(
        version=version, accepted=accepted, occurred_at=when, maturity=maturity, **changes
    )
    record_outcome(event, db, user())
    return event


@pytest.mark.parametrize(
    "maturity,value,expected",
    [
        ("unknown", True, None),
        ("provisional", True, None),
        ("provisional", None, None),
        ("final", True, True),
        ("final", False, False),
        ("withdrawn", None, None),
    ],
)
def test_only_final_assertions_resolve_business_outcomes(engine, maturity, value, expected):
    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        event = emit(db, maturity, value)
        assert record_outcome(event, db, user()).status == "duplicate"
        row = db.scalars(select(OutcomeEvent)).one()
        assert _outcome_out(row).maturity == maturity
        version = _version_from_store(db, workflow="invoice", version="v1", outcome_type="accepted")
        assert version.runs[0].accepted is expected
        events = list(db.scalars(select(ExecutionEvent)))
        assert resolve_outcomes_for_events(db, events).get(("invoice", "v1", "shared")) is expected


def test_metadata_and_technical_completion_cannot_declare_business_maturity(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        emit(db, "unknown", metadata={"maturity": "final", "completed": True})
        result = _version_from_store(db, workflow="invoice", version="v1", outcome_type="accepted")
        assert result.runs[0].accepted is None


@pytest.mark.parametrize("late_state,late_value", [("provisional", True), ("withdrawn", None)])
def test_nonfinal_revision_suppresses_prior_final_even_when_delivered_first(
    engine, late_state, late_value
):
    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        emit(db, late_state, late_value, NOW + timedelta(hours=1))
        emit(db)  # Delayed old assertion does not win by arrival order.
        result = _version_from_store(db, workflow="invoice", version="v1", outcome_type="accepted")
        assert result.runs[0].accepted is None
        assert resolve_outcomes_for_events(db, list(db.scalars(select(ExecutionEvent)))) == {}


def test_future_assertions_cannot_replace_the_current_business_state(engine):
    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        emit(db, accepted=False)
        emit(db, accepted=True, when=datetime.now(UTC) + timedelta(days=1))
        result = _version_from_store(db, workflow="invoice", version="v1", outcome_type="accepted")
        assert result.runs[0].accepted is False
        assert result.source_fingerprint.outcome_records == 1
        assert resolve_outcomes_for_events(db, list(db.scalars(select(ExecutionEvent)))) == {
            ("invoice", "v1", "shared"): False,
        }


def test_maturity_is_immutable_and_final_inference_stays_inferred(engine):
    request = VersionComparisonRequest(
        workflow="invoice",
        baseline_version="v1",
        candidate_version="v2",
        policy={"min_runs": 1, "min_success_rate": 0.5},
    )
    with Session(engine) as raw:
        db = scoped(raw)
        seed(db)
        original = emit(db, "provisional")
        with pytest.raises(HTTPException) as error:
            record_outcome(original.model_copy(update={"maturity": "final"}), db, user())
        assert error.value.status_code == 422
        emit(db, when=NOW + timedelta(minutes=1))
        seed(db, "v2")
        emit(db, version="v2", provenance="inferred")
        report = compare_versions_from_store(db, request)
        assert report.verdict == "abstain"
        assert "candidate_contains_inferred_outcomes" in report.reason_codes


def test_source_ordered_corrections_preserve_each_retained_revision(engine):
    request = VersionComparisonRequest(
        workflow="invoice",
        baseline_version="v1",
        candidate_version="v2",
        policy={"min_runs": 1, "min_success_rate": 0.5},
    )
    with Session(engine) as raw:
        db = scoped(raw)
        for version in ("v1", "v2"):
            seed(db, version)
            emit(db, version=version)

        def compare():
            return retain_decision(
                db, request, compare_versions_from_store(db, request), evaluated_by="test"
            )

        first = compare()
        emit(db, accepted=False, version="v2", when=NOW + timedelta(minutes=1))
        corrected = compare()
        emit(db, maturity="withdrawn", accepted=None, version="v2", when=NOW + timedelta(minutes=2))
        withdrawn = compare()
        assert_interval_abstention(first)
        assert [corrected.verdict, withdrawn.verdict] == ["fail", "abstain"]
        assert len({item.decision_id for item in (first, corrected, withdrawn)}) == 3
        assert corrected.source_evidence["candidate"].version == "stored-assertions/4"
        history = {item.decision_id: item for item in list_retained_decisions(db)}
        assert all(history[item.decision_id] == item for item in (first, corrected, withdrawn))
        assert len(list(db.scalars(select(OutcomeEvent)))) == 4
