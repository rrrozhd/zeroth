from sqlalchemy import case, func, select

from zeroth.econ.plane.capabilities.models import Capability
from zeroth.econ.plane.costing.models import CalibrationMetric, CostEstimate, GroundTruthCost
from zeroth.econ.plane.counterfactual.models import ValueEstimate
from zeroth.econ.plane.enforcement.models import PolicyAction
from zeroth.econ.plane.performance.models import PerformanceSnapshot
from zeroth.econ.plane.scoped_session import ScopedSession

#: Every dashboard read is a summary over recent history, so each one carries an
#: explicit bound. Without them these functions selected whole tables and
#: aggregated in Python, so memory and latency grew with total history rather
#: than with what the panel actually renders.
LEADERBOARD_ROWS = 5
TREND_POINTS = 500
TIMELINE_ROWS = 200
COMPARISON_ROWS = 20


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("dashboard persistence requires an exact ScopedSession")
    return db


def kpis(db: ScopedSession) -> dict:
    db = _require_exact_scoped_session(db)
    # One aggregate round trip instead of materialising every estimate ever
    # written. ``confidence`` is per-row arithmetic, so it is expressed in SQL
    # rather than recomputed over a fully-loaded list.
    #
    # Both clamps are ``case`` expressions on purpose. SQLite's ``max(a, b)`` is
    # a two-argument scalar; PostgreSQL's ``max`` is a one-argument aggregate, so
    # the scalar form would fail on Postgres -- and nesting it inside ``avg``
    # would be a nested aggregate there. ``case`` means the same thing on both.
    denominator = case(
        (func.abs(ValueEstimate.estimated_value_usd) > 1.0, func.abs(
            ValueEstimate.estimated_value_usd
        )),
        else_=1.0,
    )
    raw_confidence = (
        1
        - (ValueEstimate.credible_interval_high_usd - ValueEstimate.credible_interval_low_usd)
        / denominator
    )
    confidence_expr = case((raw_confidence > 0.0, raw_confidence), else_=0.0)
    row = db.execute(
        select(
            func.count(ValueEstimate.id),
            func.coalesce(func.sum(ValueEstimate.estimated_cost_usd), 0.0),
            func.coalesce(func.sum(ValueEstimate.estimated_value_usd), 0.0),
            func.coalesce(func.avg(confidence_expr), 0.0),
        )
    ).one()
    count, spend, value, confidence = int(row[0]), float(row[1]), float(row[2]), float(row[3])

    if not count:
        return {
            "total_ai_spend_usd": 0.0,
            "total_ai_value_usd": 0.0,
            "net_ai_margin_usd": 0.0,
            "portfolio_confidence_score": 0.0,
            "efficiency_index": 0.0,
        }

    return {
        "total_ai_spend_usd": spend,
        "total_ai_value_usd": value,
        "net_ai_margin_usd": value - spend,
        "portfolio_confidence_score": confidence,
        "efficiency_index": (value / spend) if spend else 0.0,
    }


def _recent_estimates(db: ScopedSession) -> list[ValueEstimate]:
    """The newest ``TREND_POINTS`` value estimates, oldest-first for plotting.

    Selecting descending under a limit and reversing keeps the chart's ascending
    order while letting the database discard the rest of the history, instead of
    streaming every estimate ever written into the process.
    """
    rows = list(
        db.execute(
            select(ValueEstimate).order_by(ValueEstimate.id.desc()).limit(TREND_POINTS)
        ).scalars()
    )
    rows.reverse()
    return rows


def _margin_leaderboard(db: ScopedSession, *, ascending: bool) -> list[dict]:
    """Return the ``LEADERBOARD_ROWS`` capabilities at one end of net margin.

    Grouped, ordered and truncated in SQL. The previous shape read every value
    estimate into a ``defaultdict`` and sorted the whole thing to keep five rows,
    and ``capital_destroyers`` re-ran that same full scan to re-sort it.
    """
    net_margin = func.coalesce(func.sum(ValueEstimate.net_margin_usd), 0.0)
    order = net_margin.asc() if ascending else net_margin.desc()
    rows = db.execute(
        select(
            ValueEstimate.capability_id,
            net_margin,
            func.coalesce(func.avg(ValueEstimate.confidence_level), 0.0),
        )
        .group_by(ValueEstimate.capability_id)
        .order_by(order)
        .limit(LEADERBOARD_ROWS)
    ).all()
    return [
        {
            "capability_id": capability_id,
            "net_margin_usd": float(margin),
            "confidence": float(confidence),
        }
        for capability_id, margin, confidence in rows
    ]


def top_creators(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    return _margin_leaderboard(db, ascending=False)


def capital_destroyers(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    return _margin_leaderboard(db, ascending=True)


def capability_ranking(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    # "Latest per capability" is one row per capability, so select the max id
    # per group and join back, rather than streaming the whole table and keeping
    # the first of each with ``setdefault``.
    latest_perf_ids = select(func.max(PerformanceSnapshot.id)).group_by(
        PerformanceSnapshot.capability_id
    )
    latest_perf: dict[str, PerformanceSnapshot] = {
        snap.capability_id: snap
        for snap in db.execute(
            select(PerformanceSnapshot).where(PerformanceSnapshot.id.in_(latest_perf_ids))
        ).scalars()
    }

    latest_est_ids = select(func.max(ValueEstimate.id)).group_by(ValueEstimate.capability_id)
    latest_est: dict[str, ValueEstimate] = {
        est.capability_id: est
        for est in db.execute(
            select(ValueEstimate).where(ValueEstimate.id.in_(latest_est_ids))
        ).scalars()
    }

    rows: list[dict] = []
    for cap in db.execute(select(Capability).where(Capability.id.in_(latest_est.keys()))).scalars():
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


def implementation_compare(db: ScopedSession, capability_id: str) -> list[dict]:
    db = _require_exact_scoped_session(db)
    rows = list(
        db.execute(
            select(ValueEstimate)
            .where(ValueEstimate.capability_id == capability_id)
            .order_by(ValueEstimate.id.desc())
            .limit(10)
        ).scalars()
    )
    out = []
    for row in rows:
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


def policy_timeline(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    out = []
    for p in db.execute(
        select(PolicyAction).order_by(PolicyAction.id.desc()).limit(TIMELINE_ROWS)
    ).scalars():
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


def confidence_trend(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    points = []
    for estimate in _recent_estimates(db):
        points.append({"x": estimate.period_end.isoformat(), "y": float(estimate.confidence_level)})
    return points


def efficiency_trend(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    points = []
    for estimate in _recent_estimates(db):
        cost = float(estimate.estimated_cost_usd)
        value = float(estimate.estimated_value_usd)
        points.append({"x": estimate.period_end.isoformat(), "y": (value / cost) if cost else 0.0})
    return points


def estimated_vs_ground_truth_cost(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    est_rows = list(
        db.execute(
            select(CostEstimate).order_by(CostEstimate.id.desc()).limit(COMPARISON_ROWS)
        ).scalars()
    )
    # Only the ground truth for the capabilities actually rendered, summed in
    # SQL -- the previous shape read every ground-truth row ever recorded and
    # re-filtered it in Python once per estimate.
    capability_ids = [row.capability_id for row in est_rows]
    gt_totals = {
        capability_id: float(total)
        for capability_id, total in db.execute(
            select(
                GroundTruthCost.capability_id,
                func.coalesce(func.sum(GroundTruthCost.amount_usd), 0.0),
            )
            .where(GroundTruthCost.capability_id.in_(capability_ids))
            .group_by(GroundTruthCost.capability_id)
        ).all()
    }

    out = []
    for row in est_rows:
        gt_total = gt_totals.get(row.capability_id, 0.0)
        out.append(
            {
                "capability_id": row.capability_id,
                "estimated_cost_usd": float(row.total_cost_estimate_usd),
                "ground_truth_cost_usd": gt_total,
            }
        )
    return out


def confidence_gate_status(db: ScopedSession) -> dict:
    db = _require_exact_scoped_session(db)
    total, passed = db.execute(
        select(
            func.count(ValueEstimate.id),
            func.coalesce(
                func.sum(case((ValueEstimate.confidence_gate_passed.is_(True), 1), else_=0)), 0
            ),
        )
    ).one()
    return {"passed": int(passed), "blocked": int(total) - int(passed)}


def data_quality_mix(db: ScopedSession) -> dict:
    db = _require_exact_scoped_session(db)
    counts = {
        quality: int(total)
        for quality, total in db.execute(
            select(CostEstimate.data_quality, func.count(CostEstimate.id)).group_by(
                CostEstimate.data_quality
            )
        ).all()
    }
    return {
        "measured": counts.get("measured", 0),
        "inferred": counts.get("inferred", 0),
        "mixed": counts.get("mixed", 0),
    }


def calibration_trend(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    # Same unbounded-scan shape as the trends above, bounded the same way.
    rows = list(
        db.execute(
            select(CalibrationMetric).order_by(CalibrationMetric.id.desc()).limit(TREND_POINTS)
        ).scalars()
    )
    rows.reverse()
    return [{"x": row.period, "y": float(row.mape)} for row in rows]


def action_suppression(db: ScopedSession) -> list[dict]:
    db = _require_exact_scoped_session(db)
    blocked = db.execute(
        select(ValueEstimate.period_end)
        .where(ValueEstimate.confidence_gate_passed.is_not(True))
        .order_by(ValueEstimate.id.desc())
        .limit(TREND_POINTS)
    ).scalars()
    return [{"x": period_end.isoformat(), "y": 1.0} for period_end in reversed(list(blocked))]


def drift_timeline(db: ScopedSession, capability_id: str) -> list[dict]:
    db = _require_exact_scoped_session(db)
    rows = list(
        db.execute(
            select(ValueEstimate)
            .where(ValueEstimate.capability_id == capability_id)
            .order_by(ValueEstimate.id.desc())
            .limit(TREND_POINTS)
        ).scalars()
    )
    rows.reverse()
    return [
        {"x": estimate.period_end.isoformat(), "y": float(estimate.drift_score)}
        for estimate in rows
    ]
