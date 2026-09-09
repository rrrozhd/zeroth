from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import select

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.costing.service import PricingCatalogReader, estimate_cost_for_period
from zeroth.econ.plane.counterfactual.models import ValuationRun, ValueEstimate
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.instrumentation.charge_costs import CostAmounts, resolve_costs
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.statistics.intervals import (
    DRIFT_CRITICAL_SCORE,
    DRIFT_WARNING_SCORE,
    MIN_BOOTSTRAP_SAMPLE,
    binary_dollar_interval,
    coefficient_of_variation,
    mean_shift_score,
    student_t_mean_interval,
)
from zeroth.econ.plane.statistics.service import (
    bootstrap_interval,
    build_confidence_breakdown,
    confidence_gate,
    relative_interval_width,
)

#: Drift thresholds in standard-error units of the recent-window mean shift; see
#: ``mean_shift_score``. The earlier ``|last - mean| / |mean|`` score fired ``critical``
#: on every binary proxy by construction (a $120/$0 conversion series scores 1.0 or
#: infinity on each observation) and read the last row of an unordered query.
_DRIFT_WARNING = DRIFT_WARNING_SCORE
_DRIFT_CRITICAL = DRIFT_CRITICAL_SCORE


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("counterfactual persistence requires an exact ScopedSession")
    return db


def _bound_tenant(db: ScopedSession) -> str:
    if db.scope is None:
        raise ValueError("counterfactual evaluation requires a tenant-bound scope")
    return db.scope.tenant_id


def _event_cost(event: ExecutionEvent, cost: CostAmounts) -> float:
    return 0.0 if event.cost_role == "summary" else float(cost.total)


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


def _binary_class_values(outcome_type: str, formula_id: str, params: dict) -> tuple[float, float]:
    """Per-class proxy dollars for a binary outcome type, taken from the formula.

    Evaluating the formula on a synthetic positive and negative outcome yields the
    exact per-class values (constants, or ``proxy_parameters``) without depending on
    which classes the sample happened to contain.
    """
    positive = SimpleNamespace(
        outcome_type=outcome_type, outcome_value="1", outcome_payload_json={}
    )
    negative = SimpleNamespace(
        outcome_type=outcome_type, outcome_value="0", outcome_payload_json={}
    )
    return (
        _proxy_value_for_outcome(positive, formula_id, params),
        _proxy_value_for_outcome(negative, formula_id, params),
    )


def _join_rate(executions: list[ExecutionEvent], outcomes: list[OutcomeEvent]) -> float:
    if not executions:
        return 0.0
    keys = {e.join_key or e.execution_id for e in executions}
    out_keys = {o.join_key or o.execution_id for o in outcomes}
    return len(keys.intersection(out_keys)) / max(len(keys), 1)


def _pick_interval(
    values: list[float],
    outcomes: list[OutcomeEvent],
    confidence: float,
    mode: str,
    formula_id: str = "default_proxy",
    proxy_params: dict | None = None,
) -> tuple[str, float, float, float]:
    """Interval for the period's proxy dollars: ``(method, estimate, low, high)``.

    ``mode`` is accepted for call compatibility; the estimator is chosen by outcome
    shape and sample size, not by evaluation mode. A single binary outcome type maps the
    Wilson band on the positive rate through the formula's per-class dollar values, so
    the interval keeps its width when one class has not been observed yet (the earlier
    observed-class-mean mapping collapsed to ``[0, 0]`` whenever no positive had been
    seen). Everything else uses the bootstrap-t from ``MIN_BOOTSTRAP_SAMPLE``
    observations and the Student-t interval below it.
    """
    del mode
    binary_types = {"conversion", "fraud_flag"}
    outcome_types = {o.outcome_type for o in outcomes}
    if outcomes and len(outcome_types) == 1 and outcome_types <= binary_types:
        positives = 0
        for o in outcomes:
            v = _parse_outcome_value(o)
            truthy = bool(v) if isinstance(v, bool) else str(v).lower() in {"true", "1"}
            positives += truthy
        v_pos, v_neg = _binary_class_values(
            next(iter(outcome_types)), formula_id, proxy_params or {}
        )
        estimate, ci_low, ci_high = binary_dollar_interval(
            positives, len(outcomes), v_pos, v_neg, confidence
        )
        return "wilson_binomial", estimate, ci_low, ci_high

    if len(values) >= MIN_BOOTSTRAP_SAMPLE:
        mu, low, high = bootstrap_interval(values, confidence=confidence)
        return "bootstrap_t", mu * len(values), low * len(values), high * len(values)

    interval = student_t_mean_interval(values, confidence)
    n = len(values)
    return interval.method, interval.mean * n, interval.low * n, interval.high * n


def _arms_summary(executions: list[ExecutionEvent], outcome_values: dict[str, float], costs: dict[int, CostAmounts]) -> dict:
    arms: dict[str, dict[str, float]] = {"A": {"value": 0.0, "cost": 0.0}, "B": {"value": 0.0, "cost": 0.0}}
    for e in executions:
        arm = str((e.event_metadata or {}).get("assigned_arm", ""))
        if arm not in arms:
            continue
        key = e.join_key or e.execution_id
        arms[arm]["cost"] += _event_cost(e, costs[e.id])
        arms[arm]["value"] += outcome_values.get(key, 0.0)
    for arm in arms:
        arms[arm]["net"] = arms[arm]["value"] - arms[arm]["cost"]
    return arms


def run_evaluation(
    db: ScopedSession,
    payload: EvaluationRunRequest,
    *,
    pricing: PricingCatalogReader | None = None,
) -> ValueEstimate:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    capability = db.execute(
        select(Capability).where(
            Capability.tenant_id == tenant_id,
            Capability.id == payload.capability_id,
        )
    ).scalar_one_or_none()
    if capability is None:
        raise ValueError("capability does not exist in the bound tenant")
    if payload.implementation_id:
        implementation = db.execute(
            select(Implementation).where(
                Implementation.tenant_id == tenant_id,
                Implementation.id == payload.implementation_id,
                Implementation.capability_id == payload.capability_id,
            )
        ).scalar_one_or_none()
        if implementation is None:
            raise ValueError(
                "implementation does not belong to the capability in the bound tenant"
            )
    valuation_config = capability.valuation_config

    exec_stmt = select(ExecutionEvent).where(
        ExecutionEvent.tenant_id == tenant_id,
        ExecutionEvent.capability_id == payload.capability_id,
        ExecutionEvent.timestamp >= payload.period_start,
        ExecutionEvent.timestamp <= payload.period_end,
    )
    if payload.implementation_id:
        exec_stmt = exec_stmt.where(ExecutionEvent.implementation_id == payload.implementation_id)
    executions = list(db.execute(exec_stmt).scalars())
    costs, _revisions = resolve_costs(db, executions)

    outcome_stmt = select(OutcomeEvent).where(
        OutcomeEvent.tenant_id == tenant_id,
        OutcomeEvent.capability_id == payload.capability_id,
        OutcomeEvent.occurred_at >= payload.period_start,
        OutcomeEvent.occurred_at <= payload.period_end,
    )
    if payload.implementation_id:
        outcome_stmt = outcome_stmt.where(
            OutcomeEvent.implementation_id == payload.implementation_id
        )
    # Time order is what makes the drift window a window; an unordered scan made
    # "the last outcome" whichever row the engine returned last.
    outcome_stmt = outcome_stmt.order_by(OutcomeEvent.occurred_at, OutcomeEvent.id)
    outcomes = list(db.execute(outcome_stmt).scalars())

    join_lookup = {e.join_key or e.execution_id: e for e in executions}
    filtered_outcomes = [o for o in outcomes if (o.join_key or o.execution_id) in join_lookup]

    if settings.stat_cost_engine:
        cost_est = estimate_cost_for_period(
            db,
            payload.capability_id,
            payload.implementation_id,
            payload.period_start,
            payload.period_end,
            pricing=pricing,
        )
        total_cost = float(cost_est.total_cost_estimate_usd)
        cost_quality = cost_est.data_quality
    else:
        total_cost = sum(_event_cost(e, costs[e.id]) for e in executions)
        states = {
            MeasurementState(costs[e.id].cost_measurement) for e in executions if e.cost_role != "summary"
        }
        cost_quality = (
            "unmeasured"
            if MeasurementState.UNMEASURED in states
            else "mixed"
            if len(states) > 1
            else next(iter(states)).value
            if states
            else "unmeasured"
        )

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

    interval_method, estimated_value, ci_low, ci_high = _pick_interval(
        proxy_values,
        filtered_outcomes,
        payload.confidence_level,
        method,
        formula_id=formula_id,
        proxy_params=proxy_params,
    )

    rel_width = relative_interval_width(estimated_value, ci_low, ci_high)
    gate_cfg = valuation_config.get("confidence_gate") or {}
    min_conf = float(gate_cfg.get("min_confidence_level", settings.confidence_gate_level))
    max_rel = float(gate_cfg.get("max_relative_width", settings.confidence_gate_rel_width))

    drift_score = mean_shift_score(proxy_values)
    drift_state = _drift_state(drift_score)

    variance = coefficient_of_variation(proxy_values)
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

    gate_passed = confidence_gate(
        payload.confidence_level,
        rel_width,
        min_conf=min_conf,
        max_rel=max_rel,
        sample_size=len(proxy_values),
    )

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

    arms = _arms_summary(executions, outcome_by_key, costs)
    method_metadata = {
        "mode": payload.mode,
        "sample_size": len(proxy_values),
        "method": method,
        "formula_id": formula_id,
        "interval_method": interval_method,
        "ab_arms": arms,
        "join_rate": join_rate,
        "drift_score_units": "standard_errors",
        "variance_metric": "coefficient_of_variation",
        "minimum_gate_sample_size": MIN_BOOTSTRAP_SAMPLE,
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


def latest_estimate(db: ScopedSession, capability_id: str) -> Optional[ValueEstimate]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    stmt = (
        select(ValueEstimate)
        .where(
            ValueEstimate.tenant_id == tenant_id,
            ValueEstimate.capability_id == capability_id,
        )
        .order_by(ValueEstimate.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def estimate_history(db: ScopedSession, capability_id: str) -> list[ValueEstimate]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    stmt = (
        select(ValueEstimate)
        .where(
            ValueEstimate.tenant_id == tenant_id,
            ValueEstimate.capability_id == capability_id,
        )
        .order_by(ValueEstimate.id.desc())
    )
    return list(db.execute(stmt).scalars())
