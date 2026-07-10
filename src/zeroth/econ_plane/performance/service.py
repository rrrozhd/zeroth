from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ_plane.capabilities.models import Capability, Implementation
from zeroth.econ_plane.connectors.service import enqueue_connector_event
from zeroth.econ_plane.config import settings
from zeroth.econ_plane.counterfactual.models import ValueEstimate
from zeroth.econ_plane.performance.models import PerformanceSnapshot


def _rule_for(capability: Capability, estimate: ValueEstimate, aer: float, confidence_width: float) -> str:
    if not bool(estimate.confidence_gate_passed):
        return "INVESTIGATION_FLAG"

    ctype = capability.capability_type
    if ctype == "REVENUE" and float(estimate.net_margin_usd) < 0:
        candidate = "THROTTLE_CANDIDATE"
    elif ctype == "COST" and aer < 1.0:
        candidate = "OPTIMIZATION_ALERT"
    elif ctype == "RISK" and confidence_width > 100.0:
        candidate = "INVESTIGATION_FLAG"
    else:
        candidate = "NONE"

    if not settings.protected_capability_guard_strict:
        return candidate

    active_impl_count = len(
        list(
            estimate_session_exec(
                estimate,
                capability,
            )
        )
    )
    if capability.is_protected:
        if candidate in {"THROTTLE_CANDIDATE", "DISABLE"}:
            return "INVESTIGATION_FLAG"
    if capability.criticality == "HIGH" and active_impl_count <= 1 and candidate in {"THROTTLE_CANDIDATE", "DISABLE"}:
        return "INVESTIGATION_FLAG"
    return candidate


def estimate_session_exec(estimate: ValueEstimate, capability: Capability) -> list[Implementation]:
    del estimate
    return [i for i in capability.implementations if i.status == "ACTIVE"]


def calculate_snapshots(db: Session) -> list[PerformanceSnapshot]:
    capabilities = list(db.execute(select(Capability)).scalars())
    snapshots: list[PerformanceSnapshot] = []

    for capability in capabilities:
        estimate = db.execute(
            select(ValueEstimate)
            .where(ValueEstimate.capability_id == capability.id)
            .order_by(ValueEstimate.id.desc())
        ).scalar_one_or_none()
        if estimate is None:
            continue

        cost = float(estimate.estimated_cost_usd) or 1e-6
        value = float(estimate.estimated_value_usd)
        net = float(estimate.net_margin_usd)

        aer = value / cost
        expected_loss = max(0.0, -net)
        capital_impact = max(1.0, cost)
        risk_adjusted_return = (value - expected_loss) / capital_impact
        overhead = cost * 0.1
        operational_drag = overhead / max(value, 1.0)

        ci_width = float(estimate.credible_interval_high_usd) - float(estimate.credible_interval_low_usd)
        rule_output = _rule_for(capability, estimate, aer, ci_width)

        snap = PerformanceSnapshot(
            tenant_id=capability.tenant_id,
            capability_id=capability.id,
            implementation_id=estimate.implementation_id,
            aer=aer,
            risk_adjusted_return=risk_adjusted_return,
            operational_drag=operational_drag,
            confidence_gate_passed=bool(estimate.confidence_gate_passed),
            confidence_breakdown=estimate.confidence_breakdown,
            rule_output=rule_output,
            captured_at=datetime.now(timezone.utc),
        )
        db.add(snap)
        if settings.connectors_enabled:
            try:
                enqueue_connector_event(
                    db,
                    tenant_id=capability.tenant_id,
                    event_type="performance.snapshot",
                    event_key=f"{capability.id}:{snap.captured_at.isoformat()}",
                    capability_id=capability.id,
                    implementation_id=estimate.implementation_id,
                    payload={
                        "aer": aer,
                        "risk_adjusted_return": risk_adjusted_return,
                        "operational_drag": operational_drag,
                        "rule_output": rule_output,
                        "confidence_gate_passed": bool(estimate.confidence_gate_passed),
                        "confidence_breakdown": estimate.confidence_breakdown,
                        "captured_at": snap.captured_at.isoformat(),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        snapshots.append(snap)

    db.commit()
    for s in snapshots:
        db.refresh(s)
    return snapshots


def latest_snapshots(db: Session) -> list[PerformanceSnapshot]:
    rows = list(db.execute(select(PerformanceSnapshot).order_by(PerformanceSnapshot.id.desc())).scalars())
    dedupe: dict[str, PerformanceSnapshot] = {}
    for row in rows:
        dedupe.setdefault(row.capability_id, row)
    return list(dedupe.values())
