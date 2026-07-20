from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.models import Capability
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.costing.service import estimate_cost_for_period
from zeroth.econ.plane.counterfactual.models import ValueEstimate, ValuationRun
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.statistics.service import (
    bootstrap_interval,
    build_confidence_breakdown,
    confidence_gate,
    hierarchical_interval,
    relative_interval_width,
    wilson_interval,
)

_DRIFT_WARNING = 0.15
_DRIFT_CRITICAL = 0.3


def _drift_state(score: float) -> str:
    if score >= _DRIFT_CRITICAL:
        return "critical"
    if score >= _DRIFT_WARNING:
        return "warning"
    return "stable"


def _parse_outcome_value(outcome: OutcomeEvent) -> bool | float | str:
    payload = outcome.outcome_payload_json or {}
    if "value" in payload:
        return payload["value"]
    if outcome.outcome_value in {"True", "true", "1"}:
        return True
    if outcome.outcome_value in {"False", "false", "0"}:
        return False
    try:
        return float(outcome.outcome_value)
    except Exception:  # noqa: BLE001
        return outcome.outcome_value


def _proxy_value_for_outcome(outcome: OutcomeEvent, formula_id: str, params: dict) -> float:
    value = _parse_outcome_value(outcome)

    if formula_id == "fraud_delta_x_loss":
        loss = float(params.get("fraud_loss_amount", params.get("loss_amount", 80.0)))
        is_fraud = bool(value) if isinstance(value, bool) else str(value).lower() in {"true", "1"}
        return -loss if is_fraud else loss

    if formula_id == "time_saved_x_labor_cost":
        mins = float(params.get("minutes_saved", 5.0))
        hourly = float(params.get("labor_cost_per_hour", 40.0))
        return (mins / 60.0) * hourly

    if formula_id == "conversion_lift_x_revenue":
        rev = float(params.get("revenue_per_conversion", 120.0))
        converted = bool(value) if isinstance(value, bool) else str(value).lower() in {"true", "1"}
        return rev if converted else 0.0

    if outcome.outcome_type == "reopen_rate":
        try:
            reopen_rate = float(value)
        except Exception:  # noqa: BLE001
            reopen_rate = 0.0
        return max(0.0, (1.0 - reopen_rate) * 100.0)

    if outcome.outcome_type == "conversion":
        converted = bool(value) if isinstance(value, bool) else str(value).lower() in {"true", "1"}
        return 120.0 if converted else 0.0

    if outcome.outcome_type == "fraud_flag":
        flagged = bool(value) if isinstance(value, bool) else str(value).lower() in {"true", "1"}
        return -80.0 if flagged else 40.0

    return 10.0


def _join_rate(executions: list[ExecutionEvent], outcomes: list[OutcomeEvent]) -> float:
    if not executions:
        return 0.0
    keys = {e.join_key or e.execution_id for e in executions}
    out_keys = {o.join_key or o.execution_id for o in outcomes}
    return len(keys.intersection(out_keys)) / max(len(keys), 1)


def _pick_interval(values: list[float], outcomes: list[OutcomeEvent], confidence: float, mode: str) -> tuple[str, float, float, float]:
    binary_types = {"conversion", "fraud_flag"}
    if outcomes and all(o.outcome_type in binary_types for o in outcomes):
        pos_values: list[float] = []
        neg_values: list[float] = []
        for value, o in zip(values, outcomes, strict=False):
            v = _parse_outcome_value(o)
            truthy = bool(v) if isinstance(v, bool) else str(v).lower() in {"true", "1"}
            (pos_values if truthy else neg_values).append(value)
        positives = len(pos_values)
        _p_mean, low_p, high_p = wilson_interval(positives, len(outcomes), confidence=confidence)
        # Re-denominate the proportion CI into DOLLARS (audit B6). This branch used
        # to return a success COUNT (p * total) as the headline value — a ~120x
        # understatement for conversion ($120/positive) and a sign flip for fraud
        # (positive count vs a net-negative dollar proxy). Each outcome is worth its
        # own proxy dollars, so the point estimate is the proxy dollar sum, and the
        # Wilson band on the positive rate is mapped through the per-group mean
        # dollar values (sorted, since a mixed-sign proxy inverts the mapping).
        total = len(outcomes)
        v_pos = (sum(pos_values) / len(pos_values)) if pos_values else 0.0
        v_neg = (sum(neg_values) / len(neg_values)) if neg_values else 0.0

        def _dollars(p: float) -> float:
            return total * (p * v_pos + (1.0 - p) * v_neg)

        ci_low, ci_high = sorted((_dollars(low_p), _dollars(high_p)))
        return "wilson_binomial", sum(values), ci_low, ci_high

    if mode == "PROXY" or len(values) >= 30:
        mu, low, high = bootstrap_interval(values, confidence=confidence)
        return "bootstrap_percentile", mu * len(values), low * len(values), high * len(values)

    mu, low, high = hierarchical_interval(values, prior_mean=0.0, confidence=confidence)
    return "hierarchical_bayes", mu * len(values), low * len(values), high * len(values)


def _arms_summary(executions: list[ExecutionEvent], outcome_values: dict[str, float]) -> dict:
    arms: dict[str, dict[str, float]] = {"A": {"value": 0.0, "cost": 0.0}, "B": {"value": 0.0, "cost": 0.0}}
    for e in executions:
        arm = str((e.event_metadata or {}).get("assigned_arm", ""))
        if arm not in arms:
            continue
        key = e.join_key or e.execution_id
        arms[arm]["cost"] += float(e.token_cost_usd + e.tool_cost_usd + e.compute_cost_usd)
        arms[arm]["value"] += outcome_values.get(key, 0.0)
    for arm in arms:
        arms[arm]["net"] = arms[arm]["value"] - arms[arm]["cost"]
    return arms


def run_evaluation(db: Session, payload: EvaluationRunRequest) -> ValueEstimate:
    capability = db.execute(select(Capability).where(Capability.id == payload.capability_id)).scalar_one_or_none()
    valuation_config = capability.valuation_config if capability else {}
    tenant_id = capability.tenant_id if capability else settings.default_tenant_id

    exec_stmt = select(ExecutionEvent).where(
        ExecutionEvent.capability_id == payload.capability_id,
        ExecutionEvent.timestamp >= payload.period_start,
        ExecutionEvent.timestamp <= payload.period_end,
    )
    if payload.implementation_id:
        exec_stmt = exec_stmt.where(ExecutionEvent.implementation_id == payload.implementation_id)
    executions = list(db.execute(exec_stmt).scalars())

    outcomes = list(
        db.execute(
            select(OutcomeEvent).where(
                OutcomeEvent.capability_id == payload.capability_id,
                OutcomeEvent.occurred_at >= payload.period_start,
                OutcomeEvent.occurred_at <= payload.period_end,
            )
        ).scalars()
    )

    join_lookup = {e.join_key or e.execution_id: e for e in executions}
    filtered_outcomes = [o for o in outcomes if (o.join_key or o.execution_id) in join_lookup]

    if settings.stat_cost_engine:
        cost_est = estimate_cost_for_period(db, payload.capability_id, payload.implementation_id, payload.period_start, payload.period_end)
        total_cost = float(cost_est.total_cost_estimate_usd)
        cost_quality = cost_est.data_quality
    else:
        total_cost = float(sum((e.token_cost_usd + e.tool_cost_usd + e.compute_cost_usd) for e in executions))
        cost_quality = "measured"

    method = str(valuation_config.get("valuation_method", "PROXY")) if settings.stat_value_engine else "PROXY"
    formula_id = str(valuation_config.get("proxy_formula_id", "default_proxy"))
    proxy_params = dict(valuation_config.get("proxy_parameters", {}))

    proxy_values: list[float] = []
    outcome_by_key: dict[str, float] = {}
    for outcome in filtered_outcomes:
        value = _proxy_value_for_outcome(outcome, formula_id, proxy_params)
        proxy_values.append(value)
        key = outcome.join_key or outcome.execution_id
        outcome_by_key[key] = outcome_by_key.get(key, 0.0) + value

    interval_method, estimated_value, ci_low, ci_high = _pick_interval(proxy_values, filtered_outcomes, payload.confidence_level, method)

    rel_width = relative_interval_width(estimated_value, ci_low, ci_high)
    gate_cfg = valuation_config.get("confidence_gate") or {}
    min_conf = float(gate_cfg.get("min_confidence_level", settings.confidence_gate_level))
    max_rel = float(gate_cfg.get("max_relative_width", settings.confidence_gate_rel_width))

    drift_score = 0.0
    if proxy_values:
        drift_score = abs((proxy_values[-1] - mean(proxy_values)) / (abs(mean(proxy_values)) + 1e-6))
    drift_state = _drift_state(drift_score)

    variance = pstdev(proxy_values) if len(proxy_values) > 1 else 0.0
    provenance_ok = sum(1 for o in filtered_outcomes if o.provenance == "MEASURED") >= max(1, int(len(filtered_outcomes) * 0.5))
    calibration_ok = True
    baseline_ok = len(executions) >= 20
    breakdown = build_confidence_breakdown(
        sample_size=len(proxy_values),
        baseline_ok=baseline_ok,
        variance=variance,
        calibration_ok=calibration_ok,
        provenance_ok=provenance_ok,
        drift_state=drift_state,
        cost_data_quality=cost_quality,
    )
    join_rate = _join_rate(executions, outcomes)
    if join_rate < 0.7:
        breakdown["reason_codes"].append("OUTCOME_JOIN_RATE_LOW")

    gate_passed = confidence_gate(payload.confidence_level, rel_width, min_conf=min_conf, max_rel=max_rel)

    run = ValuationRun(
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
        mode=payload.mode,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        run_metadata={"sample_size": len(proxy_values), "execution_count": len(executions), "method": method},
    )
    db.add(run)
    db.flush()

    arms = _arms_summary(executions, outcome_by_key)
    method_metadata = {
        "mode": payload.mode,
        "sample_size": len(proxy_values),
        "method": method,
        "formula_id": formula_id,
        "interval_method": interval_method,
        "ab_arms": arms,
        "join_rate": join_rate,
    }

    estimate = ValueEstimate(
        tenant_id=tenant_id,
        valuation_run_id=run.id,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        estimated_value_usd=estimated_value,
        estimated_cost_usd=total_cost,
        net_margin_usd=estimated_value - total_cost,
        credible_interval_low_usd=ci_low,
        credible_interval_high_usd=ci_high,
        confidence_level=payload.confidence_level,
        relative_interval_width=rel_width,
        confidence_gate_passed=gate_passed,
        estimation_method_version="v3_stat",
        cost_data_quality=cost_quality,
        value_data_quality="mixed" if filtered_outcomes else "inferred",
        drift_score=drift_score,
        drift_state=drift_state,
        confidence_breakdown=breakdown,
        interval_method=interval_method,
        method_metadata=method_metadata,
    )
    db.add(estimate)
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=tenant_id,
                event_type="evaluation.completed",
                event_key=f"{payload.capability_id}:{payload.period_end.isoformat()}:{payload.mode}",
                capability_id=payload.capability_id,
                implementation_id=payload.implementation_id,
                payload={
                    "valuation_run_id": run.id,
                    "mode": payload.mode,
                    "period_start": payload.period_start.isoformat(),
                    "period_end": payload.period_end.isoformat(),
                    "estimated_value_usd": estimated_value,
                    "estimated_cost_usd": total_cost,
                    "net_margin_usd": estimated_value - total_cost,
                    "credible_interval_low_usd": ci_low,
                    "credible_interval_high_usd": ci_high,
                    "confidence_level": payload.confidence_level,
                    "relative_interval_width": rel_width,
                    "confidence_gate_passed": gate_passed,
                    "drift_score": drift_score,
                    "drift_state": drift_state,
                    "confidence_breakdown": breakdown,
                    "interval_method": interval_method,
                    "method_metadata": method_metadata,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    db.refresh(estimate)
    return estimate


def latest_estimate(db: Session, capability_id: str) -> Optional[ValueEstimate]:
    stmt = (
        select(ValueEstimate)
        .where(ValueEstimate.capability_id == capability_id)
        .order_by(ValueEstimate.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def estimate_history(db: Session, capability_id: str) -> list[ValueEstimate]:
    stmt = select(ValueEstimate).where(ValueEstimate.capability_id == capability_id).order_by(ValueEstimate.id.desc())
    return list(db.execute(stmt).scalars())
