"""Exact, fail-closed reconciliation of sanitized campaign evidence planes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal

from .coordinator import CriterionResult
from .evidence import CorrelationIds, EvidenceStore, UnsafeEvidenceError

RunStatus = Literal["succeeded", "failed"]
ReservationState = Literal["committed", "released", "held_ambiguous"]


def _require_id(value: str, field: str) -> None:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(f"{field} must be a nonblank opaque identifier")


def _require_money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative Decimal")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_event_id: str
    operation_id: str
    run_id: str
    cost_event_id: str
    provider_request_id: str | None
    cost_usd: Decimal
    cache_hit: bool
    run_status: RunStatus
    signed: bool
    chain_verified: bool

    def __post_init__(self) -> None:
        for field in ("audit_event_id", "operation_id", "run_id", "cost_event_id"):
            _require_id(getattr(self, field), field)
        if self.provider_request_id is not None:
            _require_id(self.provider_request_id, "provider_request_id")
        _require_money(self.cost_usd, "cost_usd")
        if self.run_status not in {"succeeded", "failed"}:
            raise ValueError("invalid audit run_status")


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    reservation_id: str
    operation_id: str
    run_id: str
    state: ReservationState
    maximum_usd: Decimal
    retained_usd: Decimal

    def __post_init__(self) -> None:
        for field in ("reservation_id", "operation_id", "run_id"):
            _require_id(getattr(self, field), field)
        if self.state not in {"committed", "released", "held_ambiguous"}:
            raise ValueError("invalid reservation state")
        _require_money(self.maximum_usd, "maximum_usd")
        _require_money(self.retained_usd, "retained_usd")


@dataclass(frozen=True, slots=True)
class LocalCostEvent:
    cost_event_id: str
    audit_event_id: str
    operation_id: str
    run_id: str
    provider_request_id: str | None
    amount_usd: Decimal
    cache_hit: bool
    run_status: RunStatus
    failure_tax_usd: Decimal

    def __post_init__(self) -> None:
        for field in ("cost_event_id", "audit_event_id", "operation_id", "run_id"):
            _require_id(getattr(self, field), field)
        if self.provider_request_id is not None:
            _require_id(self.provider_request_id, "provider_request_id")
        _require_money(self.amount_usd, "amount_usd")
        _require_money(self.failure_tax_usd, "failure_tax_usd")
        if self.run_status not in {"succeeded", "failed"}:
            raise ValueError("invalid local cost run_status")


@dataclass(frozen=True, slots=True)
class RegulusExecutionEvent:
    execution_event_id: str
    cost_event_id: str
    audit_event_id: str
    operation_id: str
    run_id: str
    provider_request_id: str | None
    amount_usd: Decimal
    failure_tax_usd: Decimal
    valuation_recorded: bool
    value_usd: Decimal
    margin_usd: Decimal
    synthetic_outcome_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "execution_event_id",
            "cost_event_id",
            "audit_event_id",
            "operation_id",
            "run_id",
        ):
            _require_id(getattr(self, field), field)
        if self.provider_request_id is not None:
            _require_id(self.provider_request_id, "provider_request_id")
        for field in ("amount_usd", "failure_tax_usd"):
            _require_money(getattr(self, field), field)
        for field in ("value_usd", "margin_usd"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field} must be a finite Decimal")
        if self.synthetic_outcome_id is not None:
            _require_id(self.synthetic_outcome_id, "synthetic_outcome_id")


@dataclass(frozen=True, slots=True)
class ActionReceiptRecord:
    receipt_id: str
    audit_event_id: str
    operation_id: str
    run_id: str
    status: Literal["completed", "failed", "ambiguous"]

    def __post_init__(self) -> None:
        for field in ("receipt_id", "audit_event_id", "operation_id", "run_id"):
            _require_id(getattr(self, field), field)
        if self.status not in {"completed", "failed", "ambiguous"}:
            raise ValueError("invalid action receipt status")


@dataclass(frozen=True, slots=True)
class ProviderWindowSummary:
    """Shared-project usage; deliberately has no campaign-attribution field."""

    window_id: str
    total_usd: Decimal

    def __post_init__(self) -> None:
        _require_id(self.window_id, "window_id")
        _require_money(self.total_usd, "total_usd")


@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    audits: tuple[AuditRecord, ...]
    reservations: tuple[ReservationRecord, ...]
    local_cost_events: tuple[LocalCostEvent, ...]
    regulus_events: tuple[RegulusExecutionEvent, ...]
    action_receipts: tuple[ActionReceiptRecord, ...]
    provider_window: ProviderWindowSummary


@dataclass(frozen=True, slots=True)
class Discrepancy:
    code: str
    message: str
    identities: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    passed: bool
    audit_total_usd: Decimal
    local_total_usd: Decimal
    regulus_total_usd: Decimal
    failure_tax_usd: Decimal
    discrepancies: tuple[Discrepancy, ...]
    criteria: tuple[CriterionResult, ...]


_CRITERION_CODES: Mapping[str, frozenset[str]] = {
    "audit.provider-model-usage-cost-identity": frozenset(
        {"identity_mismatch", "missing_local_cost_event", "missing_regulus_event"}
    ),
    "audit.zero-secrets": frozenset({"unsafe_reconciliation_input"}),
    "audit.signed-chain-verifies": frozenset({"audit_chain_not_signed"}),
    "audit.operation-and-cost-identities": frozenset(
        {
            "duplicate_audit_event",
            "duplicate_cost_event",
            "duplicate_provider_call",
            "identity_mismatch",
        }
    ),
    "audit.receipts-linked": frozenset(
        {"duplicate_action_receipt", "unlinked_action_receipt", "ambiguous_action_receipt"}
    ),
    "economics.one-event-per-noncache-call": frozenset(
        {
            "duplicate_local_cost_event",
            "duplicate_provider_call",
            "duplicate_regulus_event",
            "missing_local_cost_event",
            "missing_regulus_event",
            "unexpected_local_cost_event",
            "unexpected_regulus_event",
        }
    ),
    "economics.reconciled-totals": frozenset({"local_total_mismatch", "regulus_total_mismatch"}),
    "economics.shared-provider-window-upper-bound": frozenset(
        {"provider_window_below_campaign_total"}
    ),
    "economics.failure-tax": frozenset({"missing_failure_tax", "unexpected_failure_tax"}),
    "economics.zero-value-before-valuation": frozenset(
        {"prevaluation_nonzero_value_or_margin", "valuation_missing_synthetic_outcome"}
    ),
    "economics.campaign-and-run-caps": frozenset(
        {
            "missing_reservation",
            "duplicate_reservation",
            "reservation_not_committed",
            "ambiguous_reservation_held",
            "ambiguous_reservation_not_maximum",
        }
    ),
    "stop.no-secret-artifact": frozenset({"unsafe_reconciliation_input"}),
}


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _tolerance(left: Decimal, right: Decimal) -> Decimal:
    return max(Decimal("0.000001"), max(abs(left), abs(right)) * Decimal("0.005"))


def _outside_tolerance(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) > _tolerance(left, right)


def _call_key(provider_request_id: str | None, cost_event_id: str) -> tuple[str, str]:
    """Use provider identity when known, otherwise the campaign-minted cost identity."""
    if provider_request_id is not None:
        return ("provider_request_id", provider_request_id)
    return ("cost_event_id", cost_event_id)


def _call_identity(provider_request_id: str | None, cost_event_id: str) -> dict[str, str]:
    field, value = _call_key(provider_request_id, cost_event_id)
    return {field: value}


def _duplicate_codes(
    discrepancies: list[Discrepancy],
    values: Sequence[str],
    *,
    code: str,
    field: str,
) -> None:
    for value, count in Counter(values).items():
        if count > 1:
            discrepancies.append(Discrepancy(code, f"{field} occurs {count} times", {field: value}))


def _add(
    discrepancies: list[Discrepancy],
    code: str,
    message: str,
    **identities: str,
) -> None:
    discrepancies.append(Discrepancy(code, message, identities))


def _criterion_results(
    discrepancies: Sequence[Discrepancy], evidence: str
) -> tuple[CriterionResult, ...]:
    codes = {item.code for item in discrepancies}
    unsafe = "unsafe_reconciliation_input" in codes
    return tuple(
        CriterionResult(
            criterion_id,
            ("fail" if codes.intersection(relevant) else "blocked" if unsafe else "pass"),
            (evidence,),
            "see discrepancy register" if codes.intersection(relevant) else None,
        )
        for criterion_id, relevant in _CRITERION_CODES.items()
    )


def _persist_result(
    store: EvidenceStore,
    discrepancies: Sequence[Discrepancy],
    *,
    audit_total: Decimal,
    local_total: Decimal,
    regulus_total: Decimal,
    failure_tax: Decimal,
) -> ReconciliationResult:
    register: list[dict[str, object]] = []
    for discrepancy in discrepancies:
        correlation_values = {
            field: value
            for field, value in discrepancy.identities.items()
            if field
            in {
                "operation_id",
                "run_id",
                "audit_event_id",
                "cost_event_id",
                "provider_request_id",
                "ui_action_id",
            }
        }
        discrepancy_event_id = store.append_event(
            "campaign.reconciliation.discrepancy",
            {"code": discrepancy.code, "message": discrepancy.message},
            correlation=(CorrelationIds(**correlation_values) if correlation_values else None),
        )
        register.append(
            {
                "code": discrepancy.code,
                "evidence": f"events.ndjson#{discrepancy_event_id}",
                "message": discrepancy.message,
            }
        )
    event_id = store.append_event(
        "campaign.reconciliation.completed",
        {
            "discrepancies": register,
            "passed": not discrepancies,
            "provider_window_policy": "upper_bound_only",
            "tolerance": "max(0.000001 USD, 0.5 percent)",
            "totals": {
                "audit_usd": format(audit_total, "f"),
                "failure_tax_usd": format(failure_tax, "f"),
                "local_usd": format(local_total, "f"),
                "regulus_usd": format(regulus_total, "f"),
            },
        },
    )
    evidence = f"events.ndjson#{event_id}"
    return ReconciliationResult(
        passed=not discrepancies,
        audit_total_usd=audit_total,
        local_total_usd=local_total,
        regulus_total_usd=regulus_total,
        failure_tax_usd=failure_tax,
        discrepancies=tuple(discrepancies),
        criteria=_criterion_results(discrepancies, evidence),
    )


def reconcile_campaign(store: EvidenceStore, snapshot: ReconciliationInput) -> ReconciliationResult:
    """Reconcile tagged campaign records without treating shared usage as attribution."""
    try:
        store.validate(_jsonable(asdict(snapshot)))
    except UnsafeEvidenceError:
        return _persist_result(
            store,
            (
                Discrepancy(
                    "unsafe_reconciliation_input",
                    "reconciliation input failed secret-shape validation",
                    {},
                ),
            ),
            audit_total=Decimal(0),
            local_total=Decimal(0),
            regulus_total=Decimal(0),
            failure_tax=Decimal(0),
        )

    discrepancies: list[Discrepancy] = []
    noncache_audits = tuple(item for item in snapshot.audits if not item.cache_hit)
    noncache_local = tuple(item for item in snapshot.local_cost_events if not item.cache_hit)
    audit_total = sum((item.cost_usd for item in noncache_audits), Decimal(0))
    local_total = sum((item.amount_usd for item in noncache_local), Decimal(0))
    regulus_total = sum((item.amount_usd for item in snapshot.regulus_events), Decimal(0))

    _duplicate_codes(
        discrepancies,
        [item.audit_event_id for item in snapshot.audits],
        code="duplicate_audit_event",
        field="audit_event_id",
    )
    _duplicate_codes(
        discrepancies,
        [
            item.provider_request_id
            for item in noncache_audits
            if item.provider_request_id is not None
        ],
        code="duplicate_provider_call",
        field="provider_request_id",
    )
    _duplicate_codes(
        discrepancies,
        [item.cost_event_id for item in noncache_audits],
        code="duplicate_cost_event",
        field="cost_event_id",
    )
    _duplicate_codes(
        discrepancies,
        [item.cost_event_id for item in noncache_local],
        code="duplicate_cost_event",
        field="cost_event_id",
    )
    _duplicate_codes(
        discrepancies,
        [item.execution_event_id for item in snapshot.regulus_events],
        code="duplicate_regulus_event",
        field="execution_event_id",
    )
    if not snapshot.audits or any(
        not item.signed or not item.chain_verified for item in snapshot.audits
    ):
        _add(discrepancies, "audit_chain_not_signed", "signed audit chain is incomplete")

    for reservation in snapshot.reservations:
        if reservation.state != "held_ambiguous":
            continue
        _add(
            discrepancies,
            "ambiguous_reservation_held",
            "ambiguous provider outcome remains unreconciled",
            operation_id=reservation.operation_id,
        )
        if reservation.retained_usd != reservation.maximum_usd:
            _add(
                discrepancies,
                "ambiguous_reservation_not_maximum",
                "ambiguous reservation did not retain its maximum",
                operation_id=reservation.operation_id,
            )

    audit_by_call = {
        _call_key(item.provider_request_id, item.cost_event_id): item
        for item in noncache_audits
    }
    for audit in noncache_audits:
        audit_key = _call_key(audit.provider_request_id, audit.cost_event_id)
        local_matches = [
            item
            for item in noncache_local
            if _call_key(item.provider_request_id, item.cost_event_id) == audit_key
        ]
        regulus_matches = [
            item
            for item in snapshot.regulus_events
            if _call_key(item.provider_request_id, item.cost_event_id) == audit_key
        ]
        if len(local_matches) != 1:
            code = "missing_local_cost_event" if not local_matches else "duplicate_local_cost_event"
            _add(
                discrepancies,
                code,
                "non-cache call must have exactly one local cost event",
                **_call_identity(audit.provider_request_id, audit.cost_event_id),
            )
        if len(regulus_matches) != 1:
            code = "missing_regulus_event" if not regulus_matches else "duplicate_regulus_event"
            _add(
                discrepancies,
                code,
                "non-cache call must have exactly one Regulus event",
                **_call_identity(audit.provider_request_id, audit.cost_event_id),
            )
        expected = (
            audit.audit_event_id,
            audit.operation_id,
            audit.run_id,
            audit.cost_event_id,
            audit.provider_request_id,
        )
        for match in (*local_matches, *regulus_matches):
            actual = (
                match.audit_event_id,
                match.operation_id,
                match.run_id,
                match.cost_event_id,
                match.provider_request_id,
            )
            if actual != expected:
                _add(
                    discrepancies,
                    "identity_mismatch",
                    "cost-plane identity does not match audit",
                    **_call_identity(audit.provider_request_id, audit.cost_event_id),
                )

        reservations = [
            item for item in snapshot.reservations if item.operation_id == audit.operation_id
        ]
        if not reservations:
            _add(
                discrepancies,
                "missing_reservation",
                "provider call has no reservation",
                operation_id=audit.operation_id,
            )
        elif len(reservations) > 1:
            _add(
                discrepancies,
                "duplicate_reservation",
                "operation has multiple reservations",
                operation_id=audit.operation_id,
            )
        else:
            reservation = reservations[0]
            if reservation.run_id != audit.run_id:
                _add(
                    discrepancies,
                    "identity_mismatch",
                    "reservation run does not match audit",
                    operation_id=audit.operation_id,
                )
            if reservation.state not in {"committed", "held_ambiguous"}:
                _add(
                    discrepancies,
                    "reservation_not_committed",
                    "non-cache call reservation is not committed",
                    operation_id=audit.operation_id,
                )

    anchor_calls = set(audit_by_call)
    for item in noncache_local:
        if _call_key(item.provider_request_id, item.cost_event_id) not in anchor_calls:
            _add(
                discrepancies,
                "unexpected_local_cost_event",
                "local event has no non-cache audit call",
                **_call_identity(item.provider_request_id, item.cost_event_id),
            )
    for item in snapshot.regulus_events:
        if _call_key(item.provider_request_id, item.cost_event_id) not in anchor_calls:
            _add(
                discrepancies,
                "unexpected_regulus_event",
                "Regulus event has no non-cache audit call",
                **_call_identity(item.provider_request_id, item.cost_event_id),
            )

    if _outside_tolerance(audit_total, local_total):
        _add(discrepancies, "local_total_mismatch", "Audit and local totals exceed tolerance")
    if _outside_tolerance(audit_total, regulus_total):
        _add(discrepancies, "regulus_total_mismatch", "Audit and Regulus totals exceed tolerance")
    if (
        snapshot.provider_window.total_usd
        + _tolerance(snapshot.provider_window.total_usd, audit_total)
        < audit_total
    ):
        _add(
            discrepancies,
            "provider_window_below_campaign_total",
            "shared provider window is below tagged campaign spend",
            window_id=snapshot.provider_window.window_id,
        )

    audit_status = {
        _call_key(item.provider_request_id, item.cost_event_id): item.run_status
        for item in noncache_audits
    }
    failure_tax = Decimal(0)
    for local in noncache_local:
        local_key = _call_key(local.provider_request_id, local.cost_event_id)
        status = audit_status.get(local_key, local.run_status)
        if local.run_status != status:
            _add(
                discrepancies,
                "identity_mismatch",
                "local run status disagrees with audit",
                **_call_identity(local.provider_request_id, local.cost_event_id),
            )
        expected_tax = local.amount_usd if status == "failed" else Decimal(0)
        failure_tax += expected_tax
        if status == "failed" and local.failure_tax_usd != expected_tax:
            _add(
                discrepancies,
                "missing_failure_tax",
                "failed spend is not fully classified as failure tax",
                **_call_identity(local.provider_request_id, local.cost_event_id),
            )
        elif status == "succeeded" and local.failure_tax_usd != 0:
            _add(
                discrepancies,
                "unexpected_failure_tax",
                "successful spend is classified as failure tax",
                **_call_identity(local.provider_request_id, local.cost_event_id),
            )
    for event in snapshot.regulus_events:
        event_key = _call_key(event.provider_request_id, event.cost_event_id)
        expected_tax = (
            event.amount_usd
            if audit_status.get(event_key) == "failed"
            else Decimal(0)
        )
        if event.failure_tax_usd != expected_tax:
            code = "missing_failure_tax" if expected_tax else "unexpected_failure_tax"
            _add(
                discrepancies,
                code,
                "Regulus failure-tax classification disagrees",
                **_call_identity(event.provider_request_id, event.cost_event_id),
            )
        if not event.valuation_recorded and (event.value_usd != 0 or event.margin_usd != 0):
            _add(
                discrepancies,
                "prevaluation_nonzero_value_or_margin",
                "value and margin must remain zero before valuation",
                execution_event_id=event.execution_event_id,
            )
        if event.valuation_recorded and event.synthetic_outcome_id is None:
            _add(
                discrepancies,
                "valuation_missing_synthetic_outcome",
                "valuation lacks explicit synthetic outcome",
                execution_event_id=event.execution_event_id,
            )

    _duplicate_codes(
        discrepancies,
        [item.receipt_id for item in snapshot.action_receipts],
        code="duplicate_action_receipt",
        field="receipt_id",
    )
    audits_by_id = {item.audit_event_id: item for item in snapshot.audits}
    for receipt in snapshot.action_receipts:
        audit = audits_by_id.get(receipt.audit_event_id)
        if audit is None or (audit.operation_id, audit.run_id) != (
            receipt.operation_id,
            receipt.run_id,
        ):
            _add(
                discrepancies,
                "unlinked_action_receipt",
                "action receipt is not linked to its audit operation",
                receipt_id=receipt.receipt_id,
            )
        if receipt.status == "ambiguous":
            _add(
                discrepancies,
                "ambiguous_action_receipt",
                "action receipt remains ambiguous",
                receipt_id=receipt.receipt_id,
            )

    return _persist_result(
        store,
        discrepancies,
        audit_total=audit_total,
        local_total=local_total,
        regulus_total=regulus_total,
        failure_tax=failure_tax,
    )
