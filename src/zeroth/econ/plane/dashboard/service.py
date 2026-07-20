from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.models import Capability
from zeroth.econ.plane.costing.models import CalibrationMetric, CostEstimate, GroundTruthCost
from zeroth.econ.plane.counterfactual.models import ValueEstimate
from zeroth.econ.plane.enforcement.models import PolicyAction
from zeroth.econ.plane.performance.models import PerformanceSnapshot


def kpis(db: Session) -> dict:
    estimates = list(db.execute(select(ValueEstimate)).scalars())
    spend = sum(float(e.estimated_cost_usd) for e in estimates)
    value = sum(float(e.estimated_value_usd) for e in estimates)
    margin = value - spend

    if not estimates:
        return {
            "total_ai_spend_usd": 0.0,
            "total_ai_value_usd": 0.0,
            "net_ai_margin_usd": 0.0,
            "portfolio_confidence_score": 0.0,
            "efficiency_index": 0.0,
        }

    confidences = [
        max(
            0.0,
            1
            - (
                (float(e.credible_interval_high_usd) - float(e.credible_interval_low_usd))
                / max(abs(float(e.estimated_value_usd)), 1.0)
            ),
        )
        for e in estimates
    ]
    return {
        "total_ai_spend_usd": spend,
        "total_ai_value_usd": value,
        "net_ai_margin_usd": margin,
        "portfolio_confidence_score": sum(confidences) / len(confidences),
        "efficiency_index": (value / spend) if spend else 0.0,
    }


def top_creators(db: Session) -> list[dict]:
    grouped = defaultdict(list)
    for estimate in db.execute(select(ValueEstimate)).scalars():
        grouped[estimate.capability_id].append(estimate)

    rows = []
    for capability_id, estimates in grouped.items():
        net = sum(float(e.net_margin_usd) for e in estimates)
        conf = sum(float(e.confidence_level) for e in estimates) / len(estimates)
        rows.append({"capability_id": capability_id, "net_margin_usd": net, "confidence": conf})

    return sorted(rows, key=lambda r: r["net_margin_usd"], reverse=True)[:5]


def capital_destroyers(db: Session) -> list[dict]:
    rows = top_creators(db)
    return sorted(rows, key=lambda r: r["net_margin_usd"])[:5]


def capability_ranking(db: Session) -> list[dict]:
    latest_perf: dict[str, PerformanceSnapshot] = {}
    for snap in db.execute(select(PerformanceSnapshot).order_by(PerformanceSnapshot.id.desc())).scalars():
        latest_perf.setdefault(snap.capability_id, snap)

    latest_est: dict[str, ValueEstimate] = {}
    for est in db.execute(select(ValueEstimate).order_by(ValueEstimate.id.desc())).scalars():
        latest_est.setdefault(est.capability_id, est)

    rows: list[dict] = []
    for cap in db.execute(select(Capability)).scalars():
        est = latest_est.get(cap.id)
        snap = latest_perf.get(cap.id)
        if est is None:
            continue
        rows.append(
            {
                "capability_id": cap.id,
                "capability_type": cap.capability_type,
                "net_margin_usd": float(est.net_margin_usd),
                "aer": float(snap.aer) if snap else 0.0,
                "is_protected": bool(cap.is_protected),
            }
        )
    return sorted(rows, key=lambda r: r["net_margin_usd"], reverse=True)


def implementation_compare(db: Session, capability_id: str) -> list[dict]:
    rows = list(
        db.execute(
            select(ValueEstimate)
            .where(ValueEstimate.capability_id == capability_id)
            .order_by(ValueEstimate.id.desc())
        ).scalars()
    )
    out = []
    for row in rows[:10]:
        out.append(
            {
                "capability_id": row.capability_id,
                "implementation_id": row.implementation_id or "portfolio",
                "arm": (row.method_metadata or {}).get("arm"),
                "net_margin_usd": float(row.net_margin_usd),
                "confidence_low_usd": float(row.credible_interval_low_usd),
                "confidence_high_usd": float(row.credible_interval_high_usd),
                "confidence_breakdown": row.confidence_breakdown or {},
            }
        )
    return out


def policy_timeline(db: Session) -> list[dict]:
    out = []
    for p in db.execute(select(PolicyAction).order_by(PolicyAction.id.desc())).scalars():
        out.append(
            {
                "id": p.id,
                "capability_id": p.capability_id,
                "action_type": p.action_type,
                "status": p.status,
                "proposed_at": p.proposed_at.isoformat(),
                "approved_at": p.approved_at.isoformat() if p.approved_at else None,
                "applied_at": p.applied_at.isoformat() if p.applied_at else None,
            }
        )
    return out


def confidence_trend(db: Session) -> list[dict]:
    points = []
    for estimate in db.execute(select(ValueEstimate).order_by(ValueEstimate.id)).scalars():
        points.append({"x": estimate.period_end.isoformat(), "y": float(estimate.confidence_level)})
    return points


def efficiency_trend(db: Session) -> list[dict]:
    points = []
    for estimate in db.execute(select(ValueEstimate).order_by(ValueEstimate.id)).scalars():
        cost = float(estimate.estimated_cost_usd)
        value = float(estimate.estimated_value_usd)
        points.append({"x": estimate.period_end.isoformat(), "y": (value / cost) if cost else 0.0})
    return points


def estimated_vs_ground_truth_cost(db: Session) -> list[dict]:
    est_rows = list(db.execute(select(CostEstimate).order_by(CostEstimate.id.desc())).scalars())
    gt_rows = list(db.execute(select(GroundTruthCost).order_by(GroundTruthCost.id.desc())).scalars())

    out = []
    for row in est_rows[:20]:
        gt_total = sum(float(g.amount_usd) for g in gt_rows if g.capability_id == row.capability_id)
        out.append(
            {
                "capability_id": row.capability_id,
                "estimated_cost_usd": float(row.total_cost_estimate_usd),
                "ground_truth_cost_usd": gt_total,
            }
        )
    return out


def confidence_gate_status(db: Session) -> dict:
    rows = list(db.execute(select(ValueEstimate)).scalars())
    passed = sum(1 for r in rows if bool(r.confidence_gate_passed))
    blocked = len(rows) - passed
    return {"passed": passed, "blocked": blocked}


def data_quality_mix(db: Session) -> dict:
    rows = list(db.execute(select(CostEstimate)).scalars())
    measured = sum(1 for r in rows if r.data_quality == "measured")
    inferred = sum(1 for r in rows if r.data_quality == "inferred")
    mixed = sum(1 for r in rows if r.data_quality == "mixed")
    return {"measured": measured, "inferred": inferred, "mixed": mixed}


def calibration_trend(db: Session) -> list[dict]:
    return [{"x": r.period, "y": float(r.mape)} for r in db.execute(select(CalibrationMetric).order_by(CalibrationMetric.id)).scalars()]


def action_suppression(db: Session) -> list[dict]:
    blocked = [r for r in db.execute(select(ValueEstimate).order_by(ValueEstimate.id)).scalars() if not bool(r.confidence_gate_passed)]
    return [{"x": r.period_end.isoformat(), "y": 1.0} for r in blocked]


def drift_timeline(db: Session, capability_id: str) -> list[dict]:
    points = []
    for estimate in db.execute(
        select(ValueEstimate).where(ValueEstimate.capability_id == capability_id).order_by(ValueEstimate.id)
    ).scalars():
        points.append({"x": estimate.period_end.isoformat(), "y": float(estimate.drift_score)})
    return points
