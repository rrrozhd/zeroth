"""Validate injection/recovery ordering without assigning final verdicts."""

from __future__ import annotations

from zeroth.check.faults.models import FaultEventKind, FaultName, FaultResult, FaultSpec
from zeroth.check.faults.store import FaultEvidenceStore

_REQUIRED = {
    FaultName.DUPLICATE_DELIVERY: (
        FaultEventKind.INJECTION_ARMED,
        FaultEventKind.EFFECT_MARKER_WRITTEN,
        FaultEventKind.INJECTION_REACHED,
        FaultEventKind.RECEIPT_STORED,
        FaultEventKind.RECOVERY_REACHED,
        FaultEventKind.RUN_TERMINAL,
    ),
    FaultName.TIMEOUT_AFTER_EFFECT: (
        FaultEventKind.INJECTION_ARMED,
        FaultEventKind.EFFECT_MARKER_WRITTEN,
        FaultEventKind.INJECTION_REACHED,
        FaultEventKind.AMBIGUITY_OBSERVED,
        FaultEventKind.RECOVERY_REACHED,
        FaultEventKind.RUN_TERMINAL,
    ),
    FaultName.CANCELLATION_AFTER_EFFECT: (
        FaultEventKind.INJECTION_ARMED,
        FaultEventKind.EFFECT_MARKER_WRITTEN,
        FaultEventKind.INJECTION_REACHED,
        FaultEventKind.CANCELLATION_OBSERVED,
        FaultEventKind.AMBIGUITY_OBSERVED,
        FaultEventKind.RECOVERY_REACHED,
        FaultEventKind.RUN_TERMINAL,
    ),
    FaultName.RESTART_AFTER_RECEIPT: (
        FaultEventKind.INJECTION_ARMED,
        FaultEventKind.EFFECT_MARKER_WRITTEN,
        FaultEventKind.RECEIPT_STORED,
        FaultEventKind.INJECTION_REACHED,
        FaultEventKind.PROCESS_EXITED,
        FaultEventKind.RESUME_STARTED,
        FaultEventKind.RECOVERY_REACHED,
        FaultEventKind.RUN_TERMINAL,
    ),
    FaultName.ERROR_BEFORE_EFFECT: (
        FaultEventKind.INJECTION_ARMED,
        FaultEventKind.INJECTION_REACHED,
        FaultEventKind.RECOVERY_REACHED,
        FaultEventKind.RUN_TERMINAL,
    ),
}


def validate_fault_execution(spec: FaultSpec, store: FaultEvidenceStore) -> FaultResult:
    events = [event.kind for _, event in store.events(spec)]
    required = _REQUIRED[spec.name]
    cursor = -1
    ordered = True
    for kind in required:
        try:
            cursor = events.index(kind, cursor + 1)
        except ValueError:
            ordered = False
            break
    injection = FaultEventKind.INJECTION_REACHED in events
    recovery = FaultEventKind.RECOVERY_REACHED in events
    markers = store.marker_count(spec)
    safety_violation = markers > 1
    reasons: list[str] = []
    if not injection:
        reasons.append("injection_not_observed")
    if not recovery:
        reasons.append("recovery_not_observed")
    if not ordered:
        reasons.append("invalid_event_order")
    if safety_violation:
        reasons.append("duplicate_effect_marker")
    return FaultResult(
        spec=spec,
        executed=ordered and injection and recovery,
        injection_observed=injection,
        recovery_observed=recovery,
        safety_violation=safety_violation,
        marker_count=markers,
        reason_codes=tuple(reasons),
    )
