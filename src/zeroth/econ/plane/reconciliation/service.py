from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256
import json

from sqlalchemy import select

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.costing.models import CalibrationMetric, GroundTruthCost
from zeroth.econ.plane.costing.service import (
    add_ground_truth_rows as _add_ground_truth_rows,
    compute_calibration_summary as _compute_calibration_summary,
)
from zeroth.econ.plane.debugger.service import resolve_outcomes_for_events
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.reconciliation.models import ProviderBill, ProviderCostBucket
from zeroth.econ.plane.reconciliation.schemas import (
    ProviderBillAllocation,
    ProviderBillImportRequest,
    ProviderBillReport,
    UnmatchedProviderBucket,
)
from zeroth.econ.plane.scoped_session import ScopedSession

# This alias preserves the immutable annotation spelling only. Runtime access
# is authorized independently by the exact-type boundary below.
Session = ScopedSession

MAX_RECONCILIATION_EVENTS = 50_000
_CENT_FRACTION = Decimal("0.00000001")
_LIMITATIONS = [
    "Only measured telemetry is eligible for billed-dollar allocation; estimates never become provider truth.",
    "Allocation is proportional within each provider bucket and is not a request-level invoice join.",
    "Overlapping bucket scopes remain unreconciled instead of double-counting execution evidence.",
    "Provider credits, taxes, and negative adjustments require normalized non-negative cost buckets in this version.",
]


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("reconciliation persistence requires an exact ScopedSession")
    return db


def add_ground_truth_rows(db: Session, rows: list[GroundTruthCost]) -> int:
    return _add_ground_truth_rows(_require_exact_scoped_session(db), rows)


def compute_calibration_summary(db: Session) -> list[CalibrationMetric]:
    return _compute_calibration_summary(_require_exact_scoped_session(db))


def _statement_digest(payload: ProviderBillImportRequest) -> str:
    material = payload.model_dump(mode="json")
    material["buckets"] = sorted(material["buckets"], key=lambda row: row["bucket_id"])
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(encoded.encode()).hexdigest()}"


def get_provider_bill(
    db: ScopedSession, *, provider: str, statement_id: str
) -> ProviderBill | None:
    db = _require_exact_scoped_session(db)
    return db.execute(
        select(ProviderBill).where(
            ProviderBill.provider == provider,
            ProviderBill.statement_id == statement_id,
        )
    ).scalar_one_or_none()


def provider_bill_buckets(
    db: ScopedSession, *, provider_bill_id: int
) -> list[ProviderCostBucket]:
    db = _require_exact_scoped_session(db)
    return list(
        db.execute(
            select(ProviderCostBucket)
            .where(ProviderCostBucket.provider_bill_id == provider_bill_id)
            .order_by(ProviderCostBucket.bucket_id)
        ).scalars()
    )


def import_provider_bill(
    db: ScopedSession, payload: ProviderBillImportRequest
) -> tuple[bool, ProviderBill]:
    db = _require_exact_scoped_session(db)
    digest = _statement_digest(payload)
    existing = get_provider_bill(
        db, provider=payload.provider, statement_id=payload.statement_id
    )
    if existing is not None:
        if existing.statement_digest != digest:
            raise ValueError(
                "Provider bill is immutable for this provider and statement_id"
            )
        return False, existing
    bill = ProviderBill(
        statement_id=payload.statement_id,
        provider=payload.provider,
        period_start=payload.period_start,
        period_end=payload.period_end,
        currency=payload.currency,
        billed_total_usd=payload.billed_total_usd,
        source_kind=payload.source_kind,
        statement_digest=digest,
        imported_at=datetime.now(UTC),
    )
    db.add(bill)
    db.flush()
    for bucket in payload.buckets:
        db.add(
            ProviderCostBucket(
                provider_bill_id=bill.id,
                bucket_id=bucket.bucket_id,
                period_start=bucket.period_start,
                period_end=bucket.period_end,
                amount_usd=bucket.amount_usd,
                model=bucket.model,
                provider_dimensions=bucket.provider_dimensions,
            )
        )
    db.commit()
    db.refresh(bill)
    return True, bill


def _measured_cost(event: ExecutionEvent) -> Decimal:
    if event.cost_measurement != MeasurementState.MEASURED.value:
        return Decimal("0")
    return sum(
        (
            value or Decimal("0")
            for value in (
                event.token_cost_usd,
                event.tool_cost_usd,
                event.compute_cost_usd,
            )
        ),
        Decimal("0"),
    )


def _provider_matches(event: ExecutionEvent, provider: str) -> bool:
    metadata = event.event_metadata or {}
    return str(metadata.get("provider", "")).strip().lower() == provider


def _bucket_matches(event: ExecutionEvent, bucket: ProviderCostBucket) -> bool:
    metadata = event.event_metadata or {}
    if bucket.model is not None and str(metadata.get("model", event.model_version)) != bucket.model:
        return False
    return all(str(metadata.get(key, "")) == value for key, value in bucket.provider_dimensions.items())


def _allocate(amount: Decimal, events: list[ExecutionEvent]) -> list[tuple[ExecutionEvent, Decimal]]:
    weights = [_measured_cost(event) for event in events]
    total = sum(weights, Decimal("0"))
    allocated: list[tuple[ExecutionEvent, Decimal]] = []
    running = Decimal("0")
    for index, (event, weight) in enumerate(zip(events, weights, strict=True)):
        share = (
            amount - running
            if index == len(events) - 1
            else (amount * weight / total).quantize(_CENT_FRACTION, rounding=ROUND_HALF_EVEN)
        )
        running += share
        allocated.append((event, share))
    return allocated


def provider_bill_report(
    db: ScopedSession, *, provider: str, statement_id: str
) -> ProviderBillReport | None:
    db = _require_exact_scoped_session(db)
    bill = get_provider_bill(db, provider=provider, statement_id=statement_id)
    if bill is None:
        return None
    buckets = provider_bill_buckets(db, provider_bill_id=bill.id)
    events = list(
        db.execute(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.timestamp >= bill.period_start,
                ExecutionEvent.timestamp < bill.period_end,
            )
            .order_by(ExecutionEvent.timestamp, ExecutionEvent.id)
            .limit(MAX_RECONCILIATION_EVENTS + 1)
        ).scalars()
    )
    if len(events) > MAX_RECONCILIATION_EVENTS:
        raise ValueError(
            f"Provider bill exceeds the {MAX_RECONCILIATION_EVENTS}-event request-time limit"
        )
    provider_events = [event for event in events if _provider_matches(event, provider)]
    candidates: dict[int, list[ExecutionEvent]] = {}
    memberships: dict[int, set[int]] = defaultdict(set)
    for bucket in buckets:
        matched = [
            event
            for event in provider_events
            if bucket.period_start <= event.timestamp < bucket.period_end
            and _bucket_matches(event, bucket)
            and _measured_cost(event) > 0
        ]
        candidates[bucket.id] = matched
        for event in matched:
            memberships[event.id].add(bucket.id)
    ambiguous_buckets = {
        bucket_id
        for bucket_ids in memberships.values()
        if len(bucket_ids) > 1
        for bucket_id in bucket_ids
    }
    allocated_events = [
        event
        for bucket in buckets
        if bucket.id not in ambiguous_buckets
        for event in candidates[bucket.id]
    ]
    outcome_status = resolve_outcomes_for_events(db, allocated_events)
    allocation_rows: dict[tuple[int, str, str, str], dict[str, object]] = defaultdict(
        lambda: {
            "billed": Decimal("0"),
            "telemetry": Decimal("0"),
            "runs": set(),
            "events": 0,
        }
    )
    unmatched: list[UnmatchedProviderBucket] = []
    allocated_total = Decimal("0")
    telemetry_total = Decimal("0")
    matched_bucket_count = 0
    for bucket in buckets:
        matched = candidates[bucket.id]
        if bucket.id in ambiguous_buckets:
            unmatched.append(
                UnmatchedProviderBucket(
                    bucket_id=bucket.bucket_id, reason="ambiguous_bucket_scope"
                )
            )
            continue
        if not matched:
            unmatched.append(
                UnmatchedProviderBucket(
                    bucket_id=bucket.bucket_id, reason="no_measured_telemetry"
                )
            )
            continue
        matched_bucket_count += 1
        allocated_total += bucket.amount_usd
        telemetry_total += sum((_measured_cost(event) for event in matched), Decimal("0"))
        for event, billed_share in _allocate(bucket.amount_usd, matched):
            status = outcome_status.get(event.run_id or "")
            outcome = "success" if status is True else "failure" if status is False else "unresolved"
            key = (
                bucket.id,
                event.workflow_id or "(unknown)",
                event.workflow_version or "(unknown)",
                outcome,
            )
            row = allocation_rows[key]
            row["billed"] += billed_share
            row["telemetry"] += _measured_cost(event)
            row["events"] += 1
            if event.run_id:
                row["runs"].add(event.run_id)
    bucket_by_id = {bucket.id: bucket for bucket in buckets}
    allocations = [
        ProviderBillAllocation(
            bucket_id=bucket_by_id[bucket_row_id].bucket_id,
            model=bucket_by_id[bucket_row_id].model,
            provider_dimensions=bucket_by_id[bucket_row_id].provider_dimensions,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            outcome_status=outcome,
            billed_cost_usd=data["billed"],
            telemetry_cost_usd=data["telemetry"],
            run_count=len(data["runs"]),
            event_count=data["events"],
        )
        for (
            bucket_row_id,
            workflow_id,
            workflow_version,
            outcome,
        ), data in sorted(allocation_rows.items())
    ]
    unresolved_outcome = sum(
        (row.billed_cost_usd for row in allocations if row.outcome_status == "unresolved"),
        Decimal("0"),
    )
    unreconciled = bill.billed_total_usd - allocated_total
    variance = bill.billed_total_usd - telemetry_total
    matched_event_ids = set(memberships)
    unbilled_telemetry = sum(
        (
            _measured_cost(event)
            for event in provider_events
            if event.id not in matched_event_ids
        ),
        Decimal("0"),
    )
    if unreconciled != 0:
        state = "unreconciled"
    elif unresolved_outcome != 0:
        state = "outcomes_unresolved"
    elif variance != 0:
        state = "allocated_with_variance"
    else:
        state = "reconciled"
    return ProviderBillReport(
        statement_id=bill.statement_id,
        provider=bill.provider,
        statement_digest=bill.statement_digest,
        period_start=bill.period_start,
        period_end=bill.period_end,
        currency=bill.currency,
        reconciliation_state=state,
        billed_total_usd=bill.billed_total_usd,
        allocated_billed_usd=allocated_total,
        unreconciled_billed_usd=unreconciled,
        telemetry_measured_usd=telemetry_total,
        telemetry_variance_usd=variance,
        unbilled_telemetry_usd=unbilled_telemetry,
        outcome_unresolved_usd=unresolved_outcome,
        matched_buckets=matched_bucket_count,
        unmatched_buckets=unmatched,
        allocations=allocations,
        allocation_method="measured_cost_proportional",
        limitations=_LIMITATIONS,
    )


__all__ = [
    "add_ground_truth_rows",
    "compute_calibration_summary",
    "get_provider_bill",
    "import_provider_bill",
    "provider_bill_buckets",
    "provider_bill_report",
]
