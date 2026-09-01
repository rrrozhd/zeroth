from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane import enforcement
from zeroth.econ.plane.enforcement import service as enforcement_service
from zeroth.econ.plane.enforcement.models import TenantBudget
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _api():
    models = enforcement.models
    service = enforcement_service
    assert hasattr(models, "CostReservation"), "persistent cost reservation model is missing"
    assert hasattr(service, "reserve_cost"), "atomic cost reservation service is missing"
    return models, service


def _scoped(engine, tenant_id: str = "tenant-a") -> tuple[Session, ScopedSession]:
    raw = Session(engine)
    return raw, ScopedSession(raw, TenantWideScopeContext(tenant_id=tenant_id))


@pytest.fixture
def reservation_engine(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'reservations.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    TenantBudget.__table__.create(engine)
    ExecutionEvent.__table__.create(engine)
    reservation_model = getattr(enforcement.models, "CostReservation", None)
    if reservation_model is not None:
        reservation_model.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _budget(engine, tenant_id: str, cap: str) -> None:
    with Session(engine) as db:
        db.add(
            TenantBudget(
                tenant_id=tenant_id,
                budget_cap_usd=float(cap),
                updated_at=__import__("datetime").datetime.now(),
            )
        )
        db.commit()


def test_reservation_is_tenant_scoped_persistent_and_idempotent(reservation_engine) -> None:
    models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    raw, db = _scoped(reservation_engine)
    try:
        first = service.reserve_cost(
            db,
            operation_id="verify:model-a",
            max_cost_usd=Decimal("0.20"),
            campaign_id="campaign-1",
            run_id="run-1",
            run_cap_usd=Decimal("0.25"),
        )
        replay = service.reserve_cost(
            db,
            operation_id="verify:model-a",
            max_cost_usd=Decimal("0.20"),
            campaign_id="campaign-1",
            run_id="run-1",
            run_cap_usd=Decimal("0.25"),
        )
        assert first.id == replay.id
        assert first.status == "reserved"
    finally:
        raw.close()

    with Session(reservation_engine) as persisted:
        rows = persisted.query(models.CostReservation).all()
        assert [(row.tenant_id, row.operation_id) for row in rows] == [
            ("tenant-a", "verify:model-a")
        ]


def test_provider_admission_refuses_reusing_an_existing_operation(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(
            db,
            operation_id="provider-call-1",
            max_cost_usd=Decimal("0.20"),
            require_new=True,
        )
        with pytest.raises(service.CostReservationDenied, match="already admitted"):
            service.reserve_cost(
                db,
                operation_id="provider-call-1",
                max_cost_usd=Decimal("0.20"),
                require_new=True,
            )
    finally:
        raw.close()


def test_concurrent_admission_allows_only_one_reservation_under_tenant_cap(
    reservation_engine,
) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "0.25")

    def attempt(operation_id: str) -> str:
        raw, db = _scoped(reservation_engine)
        try:
            service.reserve_cost(
                db,
                operation_id=operation_id,
                max_cost_usd=Decimal("0.20"),
            )
            return "reserved"
        except service.CostReservationDenied:
            return "denied"
        finally:
            raw.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("probe-a", "probe-b")))

    assert sorted(outcomes) == ["denied", "reserved"]


def test_run_ceiling_is_admitted_atomically_with_tenant_ceiling(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "10.00")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(
            db,
            operation_id="run-probe-a",
            max_cost_usd=Decimal("0.20"),
            run_id="run-1",
            run_cap_usd=Decimal("0.25"),
        )
        with pytest.raises(service.CostReservationDenied, match="run ceiling"):
            service.reserve_cost(
                db,
                operation_id="run-probe-b",
                max_cost_usd=Decimal("0.10"),
                run_id="run-1",
                run_cap_usd=Decimal("0.25"),
            )
    finally:
        raw.close()


def test_commit_releases_remainder_and_ambiguous_retains_max(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(db, operation_id="success", max_cost_usd=Decimal("0.20"))
        committed = service.commit_cost(
            db,
            operation_id="success",
            actual_cost_usd=Decimal("0.03"),
            cost_measurement="estimated",
            cost_event_id="event-success",
            provider_request_id="request-success",
            cleanup_status="complete",
        )
        assert committed.status == "committed"
        assert committed.held_cost_usd == Decimal("0.03")
        assert committed.released_cost_usd == Decimal("0.17")

        service.reserve_cost(db, operation_id="timeout", max_cost_usd=Decimal("0.20"))
        ambiguous = service.mark_cost_ambiguous(
            db,
            operation_id="timeout",
            provider_request_id="request-timeout",
            cleanup_status="pending_reconciliation",
        )
        assert ambiguous.status == "ambiguous"
        assert ambiguous.held_cost_usd == Decimal("0.20")
        assert ambiguous.released_cost_usd == Decimal("0")
        reconciled = service.reconcile_cost(
            db,
            operation_id="timeout",
            actual_cost_usd=Decimal("0.04"),
            cost_measurement="measured",
            provider_request_id="request-timeout",
            cleanup_status="complete",
        )
        assert reconciled.status == "committed"
        assert reconciled.held_cost_usd == Decimal("0.04")
        assert reconciled.released_cost_usd == Decimal("0.16")

        service.reserve_cost(db, operation_id="auth-failed", max_cost_usd=Decimal("0.20"))
        service.mark_cost_ambiguous(
            db,
            operation_id="auth-failed",
            cleanup_status="pending_reconciliation:credential",
        )
        not_called = service.reconcile_provider_not_called(
            db,
            operation_id="auth-failed",
            reason="credential_unresolved_before_authenticated_request",
        )
        assert not_called.status == "released"
        assert not_called.actual_cost_usd == Decimal("0")
        assert not_called.held_cost_usd == Decimal("0")
        assert not_called.released_cost_usd == Decimal("0.20")
        assert not_called.cleanup_status == "provider_not_called"
    finally:
        raw.close()


def test_stale_callbacks_cannot_overwrite_terminal_or_ambiguous_reservations(
    reservation_engine,
) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(db, operation_id="ambiguous", max_cost_usd=Decimal("0.20"))
        service.mark_cost_ambiguous(db, operation_id="ambiguous")
        with pytest.raises(service.CostReservationDenied, match="invalid reservation transition"):
            service.release_cost(db, operation_id="ambiguous")

        service.reserve_cost(db, operation_id="committed", max_cost_usd=Decimal("0.20"))
        service.commit_cost(
            db,
            operation_id="committed",
            actual_cost_usd=Decimal("0.03"),
            cost_measurement="estimated",
            cost_event_id="event-committed",
        )
        with pytest.raises(service.CostReservationDenied, match="invalid reservation transition"):
            service.mark_cost_ambiguous(db, operation_id="committed")
    finally:
        raw.close()


def test_missing_budget_and_control_plane_errors_fail_closed(reservation_engine) -> None:
    _models, service = _api()
    raw, db = _scoped(reservation_engine, "unknown-tenant")
    try:
        with pytest.raises(service.CostReservationDenied, match="tenant budget is not configured"):
            service.reserve_cost(
                db,
                operation_id="must-not-run",
                max_cost_usd=Decimal("0.01"),
            )
    finally:
        raw.close()


def test_admission_counts_existing_non_probe_execution_spend(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    with Session(reservation_engine) as raw:
        raw.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id="existing-run-cost",
                join_key="run-existing",
                timestamp=__import__("datetime").datetime.now(),
                capability_id="capability",
                implementation_id="implementation",
                model_version="model",
                token_cost_usd=Decimal("0.90"),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                cost_measurement="measured",
                usage_measurement="measured",
                latency_ms=1,
                compute_time_ms=0,
                event_metadata={},
            )
        )
        raw.commit()

    raw, db = _scoped(reservation_engine)
    try:
        with pytest.raises(service.CostReservationDenied, match="tenant ceiling"):
            service.reserve_cost(
                db,
                operation_id="new-probe",
                max_cost_usd=Decimal("0.20"),
            )
    finally:
        raw.close()


def test_probe_event_and_committed_reservation_are_not_double_counted(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "0.25")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(db, operation_id="paid-probe", max_cost_usd=Decimal("0.20"))
        service.commit_cost(
            db,
            operation_id="paid-probe",
            actual_cost_usd=Decimal("0.10"),
            cost_measurement="estimated",
            cost_event_id="probe-event",
        )
    finally:
        raw.close()

    raw, db = _scoped(reservation_engine)
    try:
        status = service.get_budget_status(db, "tenant-a")
        assert status["total_cost_usd"] == pytest.approx(0.10)
    finally:
        raw.close()

    with Session(reservation_engine) as raw:
        raw.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                campaign_id="campaign-a",
                operation_id="paid-probe",
                execution_id="probe-event",
                join_key="paid-probe",
                timestamp=__import__("datetime").datetime.now(),
                capability_id="probe",
                implementation_id="model",
                model_version="model",
                token_cost_usd=Decimal("0.10"),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                cost_measurement="estimated",
                usage_measurement="measured",
                latency_ms=1,
                compute_time_ms=0,
                event_metadata={},
            )
        )
        raw.commit()

    raw, db = _scoped(reservation_engine)
    try:
        admitted = service.reserve_cost(db, operation_id="next-probe", max_cost_usd=Decimal("0.15"))
        assert admitted.status == "reserved"
        status = service.get_budget_status(db, "tenant-a")
        assert status["total_cost_usd"] == pytest.approx(0.25)
    finally:
        raw.close()


def test_budget_status_reports_real_spend_exposure_and_synthetic_proofs_separately(
    reservation_engine,
) -> None:
    """A reservation test must never become operator-visible paid spend."""
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "10.00")

    with Session(reservation_engine) as raw:
        raw.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id="legacy-real-call",
                join_key="run-real",
                timestamp=datetime.now(UTC).replace(tzinfo=None),
                capability_id="capability",
                implementation_id="implementation",
                model_version="model",
                token_cost_usd=Decimal("0.02"),
                tool_cost_usd=Decimal("0"),
                compute_cost_usd=Decimal("0"),
                cost_measurement="measured",
                usage_measurement="measured",
                latency_ms=1,
                compute_time_ms=0,
                event_metadata={},
            )
        )
        raw.commit()

    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(db, operation_id="paid-call", max_cost_usd=Decimal("0.10"))
        service.commit_cost(
            db,
            operation_id="paid-call",
            actual_cost_usd=Decimal("0.03"),
            cost_measurement="measured",
            cost_event_id="paid-event",
        )
        service.reserve_cost(db, operation_id="pending-call", max_cost_usd=Decimal("0.10"))
        service.reserve_cost(
            db,
            operation_id="budget-proof",
            max_cost_usd=Decimal("0.10"),
            evidence_kind="synthetic_control",
        )
        service.commit_cost(
            db,
            operation_id="budget-proof",
            actual_cost_usd=Decimal("0.01"),
            cost_measurement="measured",
            cost_event_id="proof-event",
        )

        status = service.get_budget_status(db, "tenant-a")
        assert status["actual_spend_usd"] == pytest.approx(0.05)
        assert status["paid_spend_usd"] == pytest.approx(0.05)
        assert status["estimated_spend_usd"] == pytest.approx(0)
        assert status["active_exposure_usd"] == pytest.approx(0.10)
        assert status["ambiguous_exposure_usd"] == pytest.approx(0)
        assert status["budget_consumed_usd"] == pytest.approx(0.15)
        assert status["total_cost_usd"] == pytest.approx(0.15)
        assert status["synthetic_control_usd"] == pytest.approx(0.01)
    finally:
        raw.close()


def test_reservation_replay_rejects_a_different_evidence_kind(reservation_engine) -> None:
    _models, service = _api()
    _budget(reservation_engine, "tenant-a", "1.00")
    raw, db = _scoped(reservation_engine)
    try:
        service.reserve_cost(
            db,
            operation_id="same-operation",
            max_cost_usd=Decimal("0.10"),
            evidence_kind="synthetic_control",
        )
        with pytest.raises(service.CostReservationDenied, match="different reservation parameters"):
            service.reserve_cost(
                db,
                operation_id="same-operation",
                max_cost_usd=Decimal("0.10"),
                evidence_kind="production",
            )
    finally:
        raw.close()
