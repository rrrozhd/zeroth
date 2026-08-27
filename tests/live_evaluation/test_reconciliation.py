from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.reconciliation import (
    ActionReceiptRecord,
    AuditRecord,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    RegulusExecutionEvent,
    ReservationRecord,
    reconcile_campaign,
)


def _input() -> ReconciliationInput:
    audit = AuditRecord(
        audit_event_id="audit-1",
        operation_id="op-1",
        run_id="run-1",
        cost_event_id="cost-1",
        provider_request_id="provider-1",
        cost_usd=Decimal("0.000200"),
        cache_hit=False,
        run_status="succeeded",
        signed=True,
        chain_verified=True,
    )
    local = LocalCostEvent(
        cost_event_id="cost-1",
        audit_event_id="audit-1",
        operation_id="op-1",
        run_id="run-1",
        provider_request_id="provider-1",
        amount_usd=Decimal("0.000200"),
        cache_hit=False,
        run_status="succeeded",
        failure_tax_usd=Decimal("0"),
    )
    regulus = RegulusExecutionEvent(
        execution_event_id="regulus-1",
        cost_event_id="cost-1",
        audit_event_id="audit-1",
        operation_id="op-1",
        run_id="run-1",
        provider_request_id="provider-1",
        amount_usd=Decimal("0.000200"),
        failure_tax_usd=Decimal("0"),
        valuation_recorded=False,
        value_usd=Decimal("0"),
        margin_usd=Decimal("0"),
    )
    return ReconciliationInput(
        audits=(audit,),
        reservations=(
            ReservationRecord(
                reservation_id="reservation-1",
                operation_id="op-1",
                run_id="run-1",
                state="committed",
                maximum_usd=Decimal("0.25"),
                retained_usd=Decimal("0"),
            ),
        ),
        local_cost_events=(local,),
        regulus_events=(regulus,),
        action_receipts=(
            ActionReceiptRecord(
                receipt_id="receipt-1",
                audit_event_id="audit-1",
                operation_id="op-1",
                run_id="run-1",
                status="completed",
            ),
        ),
        provider_window=ProviderWindowSummary(
            window_id="shared-window-1", total_usd=Decimal("0.000200")
        ),
    )


def _codes(result) -> set[str]:
    return {item.code for item in result.discrepancies}


def test_exact_reconciliation_passes_and_persists_criterion_evidence(tmp_path: Path) -> None:
    result = reconcile_campaign(EvidenceStore(tmp_path), _input())

    assert result.passed
    assert not result.discrepancies
    assert {item.status for item in result.criteria} == {"pass"}
    assert all(item.evidence[0].startswith("events.ndjson#") for item in result.criteria)
    assert (tmp_path / "events.ndjson").is_file()


def test_duplicate_and_missing_noncache_events_fail_closed(tmp_path: Path) -> None:
    baseline = _input()
    duplicate = replace(baseline.local_cost_events[0], cost_event_id="cost-duplicate")
    snapshot = replace(
        baseline,
        local_cost_events=baseline.local_cost_events + (duplicate,),
        regulus_events=(),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert {"duplicate_local_cost_event", "missing_regulus_event"} <= _codes(result)


def test_shared_provider_window_is_only_an_upper_bound_cross_check(tmp_path: Path) -> None:
    snapshot = replace(
        _input(),
        provider_window=ProviderWindowSummary(
            window_id="shared-window-noisy", total_usd=Decimal("9.75")
        ),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert result.passed
    assert "provider_window_mismatch" not in _codes(result)


def test_rounding_within_absolute_or_relative_tolerance_passes(tmp_path: Path) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        local_cost_events=(replace(baseline.local_cost_events[0], amount_usd=Decimal("0.000201")),),
        regulus_events=(replace(baseline.regulus_events[0], amount_usd=Decimal("0.000199")),),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert result.passed


def test_ambiguous_held_reservation_blocks_acceptance(tmp_path: Path) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        reservations=(
            replace(
                baseline.reservations[0],
                state="held_ambiguous",
                retained_usd=baseline.reservations[0].maximum_usd,
            ),
        ),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert "ambiguous_reservation_held" in _codes(result)


def test_ambiguous_call_without_provider_id_correlates_by_cost_event(
    tmp_path: Path,
) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        audits=(replace(baseline.audits[0], provider_request_id=None),),
        reservations=(
            replace(
                baseline.reservations[0],
                state="held_ambiguous",
                retained_usd=baseline.reservations[0].maximum_usd,
            ),
        ),
        local_cost_events=(
            replace(baseline.local_cost_events[0], provider_request_id=None),
        ),
        regulus_events=(replace(baseline.regulus_events[0], provider_request_id=None),),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert _codes(result) == {"ambiguous_reservation_held"}


def test_multiple_unknown_provider_calls_are_not_duplicates_or_cross_matched(
    tmp_path: Path,
) -> None:
    baseline = _input()
    first_audit = replace(baseline.audits[0], provider_request_id=None)
    second_audit = replace(
        first_audit,
        audit_event_id="audit-2",
        operation_id="op-2",
        run_id="run-2",
        cost_event_id="cost-2",
    )
    first_local = replace(baseline.local_cost_events[0], provider_request_id=None)
    second_local = replace(
        first_local,
        cost_event_id="cost-2",
        audit_event_id="audit-2",
        operation_id="op-2",
        run_id="run-2",
    )
    first_regulus = replace(baseline.regulus_events[0], provider_request_id=None)
    second_regulus = replace(
        first_regulus,
        execution_event_id="regulus-2",
        cost_event_id="cost-2",
        audit_event_id="audit-2",
        operation_id="op-2",
        run_id="run-2",
    )
    first_reservation = replace(
        baseline.reservations[0],
        state="held_ambiguous",
        retained_usd=baseline.reservations[0].maximum_usd,
    )
    second_reservation = replace(
        first_reservation,
        reservation_id="reservation-2",
        operation_id="op-2",
        run_id="run-2",
    )
    snapshot = replace(
        baseline,
        audits=(first_audit, second_audit),
        reservations=(first_reservation, second_reservation),
        local_cost_events=(first_local, second_local),
        regulus_events=(first_regulus, second_regulus),
        action_receipts=(),
        provider_window=ProviderWindowSummary(
            window_id="shared-window-unknown", total_usd=Decimal("0.000400")
        ),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert _codes(result) == {"ambiguous_reservation_held"}
    assert len(
        [item for item in result.discrepancies if item.code == "ambiguous_reservation_held"]
    ) == 2


def test_unlinked_held_ambiguous_reservation_is_always_surfaced(
    tmp_path: Path,
) -> None:
    baseline = _input()
    orphan = ReservationRecord(
        reservation_id="reservation-orphan",
        operation_id="op-orphan",
        run_id="run-orphan",
        state="held_ambiguous",
        maximum_usd=Decimal("0.25"),
        retained_usd=Decimal("0.10"),
    )

    result = reconcile_campaign(
        EvidenceStore(tmp_path),
        replace(baseline, reservations=baseline.reservations + (orphan,)),
    )

    assert not result.passed
    assert {
        "ambiguous_reservation_held",
        "ambiguous_reservation_not_maximum",
    } <= _codes(result)


def test_failed_spend_must_be_reported_as_failure_tax(tmp_path: Path) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        audits=(replace(baseline.audits[0], run_status="failed"),),
        local_cost_events=(replace(baseline.local_cost_events[0], run_status="failed"),),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert "missing_failure_tax" in _codes(result)


def test_unsigned_chain_and_prevaluation_margin_fail_closed(tmp_path: Path) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        audits=(replace(baseline.audits[0], signed=False),),
        regulus_events=(replace(baseline.regulus_events[0], margin_usd=Decimal("-0.0002")),),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert {"audit_chain_not_signed", "prevaluation_nonzero_value_or_margin"} <= _codes(result)


def test_secret_shaped_input_yields_sanitized_failure_evidence(tmp_path: Path) -> None:
    baseline = _input()
    snapshot = replace(
        baseline,
        action_receipts=(
            replace(
                baseline.action_receipts[0],
                receipt_id="sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            ),
        ),
    )

    result = reconcile_campaign(EvidenceStore(tmp_path), snapshot)

    assert not result.passed
    assert "unsafe_reconciliation_input" in _codes(result)
    assert "sk-proj" not in (tmp_path / "events.ndjson").read_text()
