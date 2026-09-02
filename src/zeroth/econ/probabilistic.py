"""Risk-calibrated model-migration decisions from paired run evidence."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from math import ceil, fsum, isfinite, log, sqrt
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

FORECAST_ALGORITHM_VERSION = "nested-paired-monthly-v2-hoeffding99"
MAX_SIMULATION_WORK = 50_000_000


class MigrationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=128)
    cohort: str = Field(default="default", min_length=1, max_length=128)
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    accepted: bool
    critical_error: bool = False
    source: str = Field(min_length=1, max_length=64)

    @model_serializer(mode="wrap")
    def _preserve_unmeasured_critical_error(self, handler):
        values = handler(self)
        if "critical_error" not in self.model_fields_set:
            values.pop("critical_error", None)
        return values


class ForecastCalibrationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    forecast_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    predicted_mean: float
    predicted_low: float
    predicted_high: float
    observed: float
    observed_at: datetime

    @model_validator(mode="after")
    def _interval_is_ordered(self) -> ForecastCalibrationObservation:
        if self.predicted_low > self.predicted_high:
            raise ValueError("forecast interval endpoints must be ordered")
        return self


class MetricForecastReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    calibration_state: Literal["unknown", "calibrated", "warning", "critical"]
    drift_state: Literal["unknown", "stable", "warning", "critical"]
    interval_coverage: float | None = Field(default=None, ge=0, le=1)
    relative_bias: float | None = None
    relative_residual_shift: float | None = Field(default=None, ge=0)
    calibration_periods: int = Field(ge=0)
    assessed_at: datetime | None = None


class ForecastReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calibration_state: Literal["unknown", "calibrated", "warning", "critical"] = "unknown"
    drift_state: Literal["unknown", "stable", "warning", "critical"] = "unknown"
    interval_coverage: float | None = Field(default=None, ge=0, le=1)
    relative_bias: float | None = None
    relative_residual_shift: float | None = Field(default=None, ge=0)
    calibration_periods: int = Field(default=0, ge=0)
    assessed_at: datetime | None = None
    metrics: list[MetricForecastReadiness] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)


class MigrationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload: str = Field(min_length=1)
    incumbent_model: str = Field(min_length=1)
    candidate_model: str = Field(min_length=1)
    incumbent: list[MigrationObservation] = Field(min_length=1, max_length=5_000)
    candidate: list[MigrationObservation] = Field(min_length=1, max_length=5_000)
    period_request_counts: list[int] = Field(min_length=1, max_length=366)
    demand_horizon: Literal["month", "unknown"] = "unknown"
    readiness: ForecastReadiness = Field(default_factory=ForecastReadiness)

    @model_validator(mode="after")
    def _evidence_has_unique_positive_periods(self) -> MigrationEvidence:
        if any(value <= 0 for value in self.period_request_counts):
            raise ValueError("period_request_counts must be positive")
        for label, observations in (
            ("incumbent", self.incumbent),
            ("candidate", self.candidate),
        ):
            case_ids = [observation.case_id for observation in observations]
            if len(case_ids) != len(set(case_ids)):
                raise ValueError(f"{label} case_id values must be unique")
        incumbent_cohorts = {row.case_id: row.cohort for row in self.incumbent}
        candidate_cohorts = {row.case_id: row.cohort for row in self.candidate}
        if any(
            incumbent_cohorts[case_id] != candidate_cohorts[case_id]
            for case_id in incumbent_cohorts.keys() & candidate_cohorts.keys()
        ):
            raise ValueError("paired case cohorts must match")
        return self


class CohortRoutingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128)
    cohort_candidate_shares: dict[str, float] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _shares_are_bounded(self) -> CohortRoutingAction:
        if any(
            not cohort or not isfinite(share) or share < 0 or share > 1
            for cohort, share in self.cohort_candidate_shares.items()
        ):
            raise ValueError("cohort candidate shares must be between 0 and 1")
        if not any(self.cohort_candidate_shares.values()):
            raise ValueError("routing action must send some traffic to the candidate")
        return self


class MigrationRiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_paired_cases: int = Field(default=30, ge=1)
    candidate_shares: list[float] = Field(
        default_factory=lambda: [0.1, 0.25, 0.5, 1.0], max_length=8
    )
    routing_actions: list[CohortRoutingAction] = Field(default_factory=list, max_length=8)
    max_quality_drop: float = Field(default=0.01, ge=0, le=1)
    max_p95_latency_ms: int = Field(default=2_000, ge=0)
    max_critical_error_rate: float = Field(default=0.005, gt=0, le=1)
    max_constraint_breach_probability: float = Field(default=0.05, ge=0, le=1)
    cvar_confidence: float = Field(default=0.95, gt=0, lt=1)
    max_cvar_loss_usd: Decimal = Field(default=Decimal("0"))
    critical_error_penalty_usd: Decimal = Field(default=Decimal("0"), ge=0)
    require_calibrated_forecast: bool = True
    allow_drift_warning: bool = False

    @model_validator(mode="after")
    def _shares_are_ordered_and_bounded(self) -> MigrationRiskPolicy:
        if not self.candidate_shares:
            raise ValueError("candidate_shares must not be empty")
        if any(not isfinite(share) or share <= 0 or share > 1 for share in self.candidate_shares):
            raise ValueError("candidate_shares must be greater than 0 and at most 1")
        if self.candidate_shares != sorted(set(self.candidate_shares)):
            raise ValueError("candidate_shares must be unique and increasing")
        if self.routing_actions and len(self.routing_actions) > 8:
            raise ValueError("routing_actions may contain at most 8 items")
        action_ids = [action.action_id for action in self.routing_actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("routing action_id values must be unique")
        return self


class MigrationActionForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = "global"
    candidate_share: float
    cohort_candidate_shares: dict[str, float] = Field(default_factory=dict)
    expected_monthly_cost_usd: Decimal
    monthly_cost_p05_usd: Decimal
    monthly_cost_p95_usd: Decimal
    expected_monthly_savings_usd: Decimal
    monthly_savings_p05_usd: Decimal
    monthly_savings_p95_usd: Decimal
    probability_negative_savings: float
    expected_success_rate: float | None = Field(default=None, ge=0, le=1)
    success_rate_p05: float | None = Field(default=None, ge=0, le=1)
    success_rate_p95: float | None = Field(default=None, ge=0, le=1)
    expected_p95_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_p05_ms: float | None = Field(default=None, ge=0)
    p95_latency_p95_ms: float | None = Field(default=None, ge=0)
    expected_critical_error_rate: float | None = Field(default=None, ge=0, le=1)
    critical_error_rate_p05: float | None = Field(default=None, ge=0, le=1)
    critical_error_rate_p95: float | None = Field(default=None, ge=0, le=1)
    probability_quality_breach: float
    probability_latency_breach: float
    probability_critical_error_breach: float
    value_at_risk_usd: Decimal
    cvar_loss_usd: Decimal
    minimum_quality_drop_tolerance: float
    minimum_p95_latency_limit_ms: int
    minimum_critical_error_rate_limit: float
    minimum_cvar_loss_limit_usd: Decimal
    feasible: bool
    violated_constraints: list[str]


class ProbabilisticMigrationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload: str
    incumbent_model: str
    candidate_model: str
    verdict: Literal["recommend", "hold", "abstain"]
    recommended_action: Literal[
        "ship_candidate", "hybrid_route", "cohort_route", "keep_incumbent", "collect_evidence"
    ]
    recommended_candidate_share: float
    recommended_routing: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str]
    additional_cases_required: int = 0
    simulations: int
    seed: int
    actions: list[MigrationActionForecast]
    forecast_readiness: ForecastReadiness
    evidence_lineage: dict[str, object] = Field(default_factory=dict)
    decision_id: str | None = None
    evaluated_at: datetime | None = None


def empirical_var_cvar(losses: list[float], *, confidence: float) -> tuple[float, float]:
    """Return the empirical loss cutoff and average of the declared worst tail."""
    if not losses:
        raise ValueError("losses must not be empty")
    if not isfinite(confidence) or confidence <= 0 or confidence >= 1:
        raise ValueError("confidence must be greater than 0 and less than 1")
    ordered = sorted(float(loss) for loss in losses)
    if not all(isfinite(loss) for loss in ordered):
        raise ValueError("losses must be finite")
    value_at_risk_index = max(0, min(len(ordered) - 1, ceil(confidence * len(ordered)) - 1))
    # Integrate exactly the declared upper-tail mass, including its boundary atom.
    tail_mass = (1 - confidence) * len(ordered)
    whole_rows = int(tail_mass)
    boundary_mass = tail_mass - whole_rows
    tail_sum = fsum(ordered[len(ordered) - whole_rows :])
    if boundary_mass:
        tail_sum += boundary_mass * ordered[len(ordered) - whole_rows - 1]
    return ordered[value_at_risk_index], tail_sum / tail_mass


def assess_forecast_readiness(
    observations: list[ForecastCalibrationObservation],
    *,
    required_metrics: set[str] | None = None,
    minimum_periods: int = 6,
    minimum_interval_coverage: float = 0.9,
    max_relative_bias: float = 0.1,
    max_relative_residual_shift: float = 0.2,
) -> ForecastReadiness:
    """Assess time-ordered interval coverage, bias, and recent residual drift."""
    if minimum_periods < 2:
        raise ValueError("minimum_periods must be at least 2")
    by_metric: dict[str, list[ForecastCalibrationObservation]] = {}
    seen: dict[tuple[str, str], ForecastCalibrationObservation] = {}
    for observation in observations:
        identity = (observation.metric, observation.forecast_id)
        if identity in seen:
            if seen[identity] != observation:
                raise ValueError("conflicting observations for one forecast and metric")
            continue
        seen[identity] = observation
        by_metric.setdefault(observation.metric, []).append(observation)
    expected = required_metrics if required_metrics is not None else set(by_metric)
    missing_metrics = sorted(expected - by_metric.keys())
    metric_readiness = [
        _assess_metric_readiness(
            metric,
            rows,
            minimum_periods=minimum_periods,
            minimum_interval_coverage=minimum_interval_coverage,
            max_relative_bias=max_relative_bias,
            max_relative_residual_shift=max_relative_residual_shift,
        )
        for metric, rows in sorted(by_metric.items())
    ]
    if not metric_readiness:
        return ForecastReadiness(missing_metrics=missing_metrics)
    calibration_rank = {"calibrated": 0, "warning": 1, "unknown": 2, "critical": 3}
    drift_rank = {"stable": 0, "warning": 1, "unknown": 2, "critical": 3}
    worst_calibration = max(
        metric_readiness, key=lambda row: calibration_rank[row.calibration_state]
    ).calibration_state
    worst_drift = max(metric_readiness, key=lambda row: drift_rank[row.drift_state]).drift_state
    if missing_metrics:
        worst_calibration = "unknown"
        worst_drift = "unknown"
    coverage_values = [
        row.interval_coverage for row in metric_readiness if row.interval_coverage is not None
    ]
    bias_values = [row.relative_bias for row in metric_readiness if row.relative_bias is not None]
    shift_values = [
        row.relative_residual_shift
        for row in metric_readiness
        if row.relative_residual_shift is not None
    ]
    return ForecastReadiness(
        calibration_state=worst_calibration,
        drift_state=worst_drift,
        interval_coverage=min(coverage_values) if coverage_values else None,
        relative_bias=(max(bias_values, key=abs) if bias_values else None),
        relative_residual_shift=max(shift_values) if shift_values else None,
        calibration_periods=min(row.calibration_periods for row in metric_readiness),
        assessed_at=max(row.assessed_at for row in metric_readiness if row.assessed_at is not None),
        metrics=metric_readiness,
        missing_metrics=missing_metrics,
    )


def _assess_metric_readiness(
    metric: str,
    observations: list[ForecastCalibrationObservation],
    *,
    minimum_periods: int,
    minimum_interval_coverage: float,
    max_relative_bias: float,
    max_relative_residual_shift: float,
) -> MetricForecastReadiness:
    ordered = sorted(observations, key=lambda row: row.observed_at)
    if len(ordered) < minimum_periods:
        return MetricForecastReadiness(
            metric=metric,
            calibration_state="unknown",
            drift_state="unknown",
            calibration_periods=len(ordered),
            assessed_at=ordered[-1].observed_at,
        )
    coverage = fmean(row.predicted_low <= row.observed <= row.predicted_high for row in ordered)
    residuals = [row.observed - row.predicted_mean for row in ordered]
    scale = max(fmean(abs(row.observed) for row in ordered), 1e-9)
    relative_bias = fmean(residuals) / scale
    split = len(ordered) // 2
    historical_bias = fmean(residuals[:split]) / scale
    recent_bias = fmean(residuals[split:]) / scale
    residual_shift = abs(recent_bias - historical_bias)
    if residual_shift > max_relative_residual_shift:
        drift_state = "critical"
    elif residual_shift > max_relative_residual_shift / 2:
        drift_state = "warning"
    else:
        drift_state = "stable"
    severe_calibration_failure = (
        coverage < minimum_interval_coverage * 0.8 or abs(relative_bias) > max_relative_bias * 2
    )
    if severe_calibration_failure or drift_state == "critical":
        calibration_state = "critical"
    elif (
        coverage < minimum_interval_coverage
        or abs(relative_bias) > max_relative_bias
        or drift_state == "warning"
    ):
        calibration_state = "warning"
    else:
        calibration_state = "calibrated"
    return MetricForecastReadiness(
        metric=metric,
        calibration_state=calibration_state,
        drift_state=drift_state,
        interval_coverage=round(coverage, 6),
        relative_bias=round(relative_bias, 6),
        relative_residual_shift=round(residual_shift, 6),
        calibration_periods=len(ordered),
        assessed_at=ordered[-1].observed_at,
    )


def _money(value: float) -> Decimal:
    # Presentation rounding belongs in renderers, not policy or action selection.
    return Decimal(str(value))


def _empirical_quantile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    if not isfinite(quantile) or not 0 <= quantile <= 1:
        raise ValueError("quantile must be finite and between 0 and 1")
    if not all(isfinite(value) for value in values):
        raise ValueError("values must be finite")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _quantile(values: list[float], quantile: float) -> float:
    return _empirical_quantile(values, quantile)


def _paired_observations(
    evidence: MigrationEvidence,
) -> tuple[list[MigrationObservation], list[MigrationObservation]]:
    incumbent_by_id = {observation.case_id: observation for observation in evidence.incumbent}
    candidate_by_id = {observation.case_id: observation for observation in evidence.candidate}

    # IDs establish pairing, but arbitrary ID spelling must not steer seeded draws.
    # Keep duplicate-valued independent units; only their ordering is canonicalized.
    def outcome_key(row: MigrationObservation) -> tuple:
        return (row.cohort, row.cost_usd, row.latency_ms, row.accepted, row.critical_error)

    paired_ids = sorted(
        incumbent_by_id.keys() & candidate_by_id.keys(),
        key=lambda case_id: (
            outcome_key(incumbent_by_id[case_id]),
            outcome_key(candidate_by_id[case_id]),
        ),
    )
    return (
        [incumbent_by_id[case_id] for case_id in paired_ids],
        [candidate_by_id[case_id] for case_id in paired_ids],
    )


def _abstention(
    evidence: MigrationEvidence,
    *,
    policy: MigrationRiskPolicy,
    simulations: int,
    seed: int,
    reason: str,
    additional_cases_required: int,
) -> ProbabilisticMigrationDecision:
    return ProbabilisticMigrationDecision(
        workload=evidence.workload,
        incumbent_model=evidence.incumbent_model,
        candidate_model=evidence.candidate_model,
        verdict="abstain",
        recommended_action="collect_evidence",
        recommended_candidate_share=0.0,
        reason_codes=[reason],
        additional_cases_required=additional_cases_required,
        simulations=simulations,
        seed=seed,
        actions=[],
        forecast_readiness=evidence.readiness,
    )


def recommend_model_migration(
    evidence: MigrationEvidence,
    *,
    policy: MigrationRiskPolicy,
    simulations: int = 10_000,
    seed: int = 7,
) -> ProbabilisticMigrationDecision:
    """Return experimental diagnostics, never an unapproved predictive authorization."""
    diagnostic = _diagnose_model_migration(
        evidence,
        policy=policy,
        simulations=simulations,
        seed=seed,
    )
    lineage = {
        **diagnostic.evidence_lineage,
        "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
        "forecast_status": "experimental",
        "uncertainty_kind": "paired_bootstrap_monthly_demand_and_future_requests",
        "future_request_variability_included": bool(diagnostic.actions),
        "predictive_reliability": "unapproved",
    }
    if diagnostic.verdict == "abstain" and not diagnostic.actions:
        return diagnostic.model_copy(update={"evidence_lineage": lineage})
    return diagnostic.model_copy(
        update={
            "verdict": "abstain",
            "recommended_action": "collect_evidence",
            "recommended_candidate_share": 0.0,
            "recommended_routing": {},
            "reason_codes": [
                "experimental_predictive_reliability_unapproved",
                "diagnostic_nested_bootstrap_not_validated",
                *(diagnostic.reason_codes if diagnostic.verdict == "abstain" else []),
            ],
            "evidence_lineage": lineage,
        }
    )


def _breach_probability_interval(
    probability: float, *, simulations: int, action_count: int, limit: float
) -> dict[str, float | str]:
    """Fixed-N simultaneous numerical bounds, not predictive-risk certification."""
    if simulations <= 0 or action_count <= 0 or not 0 <= probability <= 1 or not 0 <= limit <= 1:
        raise ValueError("invalid probability-bound inputs")
    radius = sqrt(log(2 * 3 * action_count / 0.01) / (2 * simulations))
    lower, upper = max(0.0, probability - radius), min(1.0, probability + radius)
    status = "qualified" if upper <= limit else "infeasible" if lower > limit else "indeterminate"
    return {"lower": lower, "upper": upper, "radius": radius, "status": status}


def _histogram_quantile(counts: dict[int, int], total: int, probability: float) -> int:
    """Exact order statistic of integer request counts without expanding demand."""
    cutoff = max(1, ceil(total * probability))
    cumulative = 0
    for value, count in sorted(counts.items()):
        cumulative += count
        if cumulative >= cutoff:
            return value
    raise ValueError("histogram does not contain requested count")


def _counted_outcomes(rows, counts, demand):
    cost = fsum(float(rows[index].cost_usd) * count for index, count in counts.items())
    accepted = sum(rows[index].accepted * count for index, count in counts.items())
    critical = sum(rows[index].critical_error * count for index, count in counts.items())
    latencies: dict[int, int] = defaultdict(int)
    for index, count in counts.items():
        latencies[rows[index].latency_ms] += count
    return cost, accepted, critical, _histogram_quantile(latencies, demand, 0.95)


def _point_winner(actions):
    """Minimum-cost mathematically feasible saving action; no numerical certificate."""
    worthwhile = [a for a in actions if a.feasible and a.expected_monthly_savings_usd > 0]
    return min(
        worthwhile,
        default=None,
        key=lambda action: (
            action.expected_monthly_cost_usd,
            action.candidate_share,
            tuple(sorted(action.cohort_candidate_shares.items())),
            action.action_id,
        ),
    )


def _diagnose_model_migration(
    evidence: MigrationEvidence,
    *,
    policy: MigrationRiskPolicy,
    simulations: int = 10_000,
    seed: int = 7,
) -> ProbabilisticMigrationDecision:
    """Internal mathematical diagnostic; not a public migration authorization."""
    if simulations < 100:
        raise ValueError("simulations must be at least 100")
    if evidence.demand_horizon != "month":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="demand_horizon_unknown",
            additional_cases_required=0,
        )
    incumbent_ids = {row.case_id for row in evidence.incumbent}
    candidate_ids = {row.case_id for row in evidence.candidate}
    if incumbent_ids != candidate_ids:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="paired_outcomes_missing",
            additional_cases_required=len(incumbent_ids ^ candidate_ids),
        )
    if any(
        "critical_error" not in row.model_fields_set
        for row in [*evidence.incumbent, *evidence.candidate]
    ):
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="critical_error_measurement_missing",
            additional_cases_required=0,
        )
    readiness = evidence.readiness
    if readiness.drift_state == "critical":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="calibration_drift_critical",
            additional_cases_required=0,
        )
    if readiness.drift_state == "warning" and not policy.allow_drift_warning:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="calibration_drift_warning",
            additional_cases_required=0,
        )
    if policy.require_calibrated_forecast and readiness.calibration_state != "calibrated":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="forecast_not_calibrated",
            additional_cases_required=0,
        )
    incumbent, candidate = _paired_observations(evidence)
    paired_cases = len(incumbent)
    if paired_cases < policy.min_paired_cases:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="paired_cases_below_minimum",
            additional_cases_required=policy.min_paired_cases - paired_cases,
        )

    candidate_critical_errors = sum(observation.critical_error for observation in candidate)
    if candidate_critical_errors == 0:
        required_cases = ceil(3 / policy.max_critical_error_rate)
        if paired_cases < required_cases:
            return _abstention(
                evidence,
                policy=policy,
                simulations=simulations,
                seed=seed,
                reason="critical_error_upper_bound_too_wide",
                additional_cases_required=required_cases - paired_cases,
            )
    if policy.routing_actions:
        for cohort in sorted(
            {
                cohort
                for action in policy.routing_actions
                for cohort, share in action.cohort_candidate_shares.items()
                if share > 0
            }
        ):
            cohort_rows = [row for row in candidate if row.cohort == cohort]
            if not cohort_rows:
                return _abstention(
                    evidence,
                    policy=policy,
                    simulations=simulations,
                    seed=seed,
                    reason=f"cohort_evidence_missing:{cohort}",
                    additional_cases_required=policy.min_paired_cases,
                )
            if not any(row.critical_error for row in cohort_rows):
                required_cases = ceil(3 / policy.max_critical_error_rate)
                if len(cohort_rows) < required_cases:
                    return _abstention(
                        evidence,
                        policy=policy,
                        simulations=simulations,
                        seed=seed,
                        reason=f"cohort_critical_error_upper_bound_too_wide:{cohort}",
                        additional_cases_required=required_cases - len(cohort_rows),
                    )

    bootstrap_cases = paired_cases
    routing_specs: list[tuple[str, float, dict[str, float]]] = []
    if policy.routing_actions:
        cohort_counts: dict[str, int] = defaultdict(int)
        for row in incumbent:
            cohort_counts[row.cohort] += 1
        for action in policy.routing_actions:
            effective_share = (
                sum(
                    count * action.cohort_candidate_shares.get(cohort, 0.0)
                    for cohort, count in cohort_counts.items()
                )
                / paired_cases
            )
            routing_specs.append(
                (action.action_id, effective_share, action.cohort_candidate_shares)
            )
    else:
        routing_specs = [(f"global-{share:g}", share, {}) for share in policy.candidate_shares]
    planned_work = simulations * (
        paired_cases + max(evidence.period_request_counts) * (1 + len(routing_specs))
    )
    if planned_work > MAX_SIMULATION_WORK:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="simulation_work_budget_exceeded",
            additional_cases_required=0,
        ).model_copy(
            update={
                "evidence_lineage": {
                    "planned_simulation_work": planned_work,
                    "simulation_work_limit": MAX_SIMULATION_WORK,
                    "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
                }
            }
        )
    rng = random.Random(seed)
    paired_rows = incumbent + candidate
    scenario_results: dict[str, dict[str, list[float]]] = {
        action_id: {
            "cost": [],
            "savings": [],
            "loss": [],
            "quality_breach": [],
            "latency_breach": [],
            "critical_breach": [],
            "quality_drop": [],
            "success_rate": [],
            "p95_latency": [],
            "critical_rate": [],
        }
        for action_id, _, _ in routing_specs
    }
    for _ in range(simulations):
        sampled_indices = [rng.randrange(paired_cases) for _ in range(bootstrap_cases)]
        demand = rng.choice(evidence.period_request_counts)
        baseline_counts: Counter[int] = Counter()
        routed_counts = [Counter() for _ in routing_specs]
        # Each draw is one whole future request, from the paired outer law.
        # A common routing uniform couples actions without changing their marginals.
        for _request in range(demand):
            index = sampled_indices[rng.randrange(bootstrap_cases)]
            uniform = rng.random()
            baseline_counts[index] += 1
            for counts, (_, share, cohort_shares) in zip(routed_counts, routing_specs, strict=True):
                threshold = (
                    cohort_shares.get(incumbent[index].cohort, 0.0) if cohort_shares else share
                )
                counts[index + paired_cases if uniform < threshold else index] += 1
        baseline_cost = fsum(float(incumbent[i].cost_usd) * n for i, n in baseline_counts.items())
        baseline_accepted = sum(incumbent[i].accepted * n for i, n in baseline_counts.items())
        baseline_critical = sum(incumbent[i].critical_error * n for i, n in baseline_counts.items())
        for (action_id, _share, _cohort_shares), counts in zip(
            routing_specs, routed_counts, strict=True
        ):
            values = scenario_results[action_id]
            action_cost, action_accepted, action_critical, action_p95_latency = _counted_outcomes(
                paired_rows, counts, demand
            )
            action_success_rate = action_accepted / demand
            action_critical_rate = action_critical / demand
            quality_drop = (baseline_accepted - action_accepted) / demand
            incremental_critical_errors = action_critical - baseline_critical
            loss = (
                action_cost
                - baseline_cost
                + float(policy.critical_error_penalty_usd) * incremental_critical_errors
            )

            values["cost"].append(action_cost)
            values["savings"].append(baseline_cost - action_cost)
            values["loss"].append(loss)
            values["quality_breach"].append(float(quality_drop > policy.max_quality_drop))
            values["latency_breach"].append(float(action_p95_latency > policy.max_p95_latency_ms))
            values["critical_breach"].append(
                float(action_critical_rate > policy.max_critical_error_rate)
            )
            values["quality_drop"].append(quality_drop)
            values["success_rate"].append(action_success_rate)
            values["p95_latency"].append(action_p95_latency)
            values["critical_rate"].append(action_critical_rate)

    actions: list[MigrationActionForecast] = []
    for action_id, share, cohort_shares in routing_specs:
        values = scenario_results[action_id]
        probability_quality_breach = fmean(values["quality_breach"])
        probability_latency_breach = fmean(values["latency_breach"])
        probability_critical_breach = fmean(values["critical_breach"])
        value_at_risk, cvar = empirical_var_cvar(values["loss"], confidence=policy.cvar_confidence)
        admissible_quantile = 1 - policy.max_constraint_breach_probability
        violated: list[str] = []
        breach_limit = policy.max_constraint_breach_probability
        if probability_quality_breach > breach_limit:
            violated.append("quality_chance_constraint")
        if probability_latency_breach > breach_limit:
            violated.append("latency_chance_constraint")
        if probability_critical_breach > breach_limit:
            violated.append("critical_error_chance_constraint")
        if cvar > float(policy.max_cvar_loss_usd):
            violated.append("cvar_loss_constraint")
        actions.append(
            MigrationActionForecast(
                action_id=action_id,
                candidate_share=share,
                cohort_candidate_shares=cohort_shares,
                expected_monthly_cost_usd=_money(fmean(values["cost"])),
                monthly_cost_p05_usd=_money(_empirical_quantile(values["cost"], 0.05)),
                monthly_cost_p95_usd=_money(_empirical_quantile(values["cost"], 0.95)),
                expected_monthly_savings_usd=_money(fmean(values["savings"])),
                monthly_savings_p05_usd=_money(_empirical_quantile(values["savings"], 0.05)),
                monthly_savings_p95_usd=_money(_empirical_quantile(values["savings"], 0.95)),
                probability_negative_savings=fmean(
                    float(savings < 0) for savings in values["savings"]
                ),
                expected_success_rate=fmean(values["success_rate"]),
                success_rate_p05=_empirical_quantile(values["success_rate"], 0.05),
                success_rate_p95=_empirical_quantile(values["success_rate"], 0.95),
                expected_p95_latency_ms=fmean(values["p95_latency"]),
                p95_latency_p05_ms=_empirical_quantile(values["p95_latency"], 0.05),
                p95_latency_p95_ms=_empirical_quantile(values["p95_latency"], 0.95),
                expected_critical_error_rate=fmean(values["critical_rate"]),
                critical_error_rate_p05=_empirical_quantile(values["critical_rate"], 0.05),
                critical_error_rate_p95=_empirical_quantile(values["critical_rate"], 0.95),
                probability_quality_breach=probability_quality_breach,
                probability_latency_breach=probability_latency_breach,
                probability_critical_error_breach=probability_critical_breach,
                value_at_risk_usd=_money(value_at_risk),
                cvar_loss_usd=_money(cvar),
                minimum_quality_drop_tolerance=_empirical_quantile(
                    values["quality_drop"], admissible_quantile
                ),
                minimum_p95_latency_limit_ms=ceil(
                    _empirical_quantile(values["p95_latency"], admissible_quantile)
                ),
                minimum_critical_error_rate_limit=_empirical_quantile(
                    values["critical_rate"], admissible_quantile
                ),
                minimum_cvar_loss_limit_usd=_money(cvar),
                feasible=not violated,
                violated_constraints=violated,
            )
        )

    qualifications = {
        action.action_id: {
            metric: _breach_probability_interval(
                probability,
                simulations=simulations,
                action_count=len(actions),
                limit=policy.max_constraint_breach_probability,
            )
            for metric, probability in (
                ("quality", action.probability_quality_breach),
                ("latency", action.probability_latency_breach),
                ("critical_error", action.probability_critical_error_breach),
            )
        }
        for action in actions
    }
    lineage = {
        "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
        "planned_simulation_work": planned_work,
        "simulation_work_limit": MAX_SIMULATION_WORK,
        "numerical_qualification": {
            "method": "fixed_N_Hoeffding",
            "familywise_confidence": 0.99,
            "metric_action_comparisons": 3 * len(actions),
            "simulations": simulations,
            "actions": qualifications,
            "scope": (
                "per-metric MC probability only; excludes CVaR, model risk "
                "and joint any-breach risk"
            ),
        },
    }
    point_winner = _point_winner(actions)
    if point_winner is None:
        return ProbabilisticMigrationDecision(
            workload=evidence.workload,
            incumbent_model=evidence.incumbent_model,
            candidate_model=evidence.candidate_model,
            verdict="hold",
            recommended_action="keep_incumbent",
            recommended_candidate_share=0.0,
            reason_codes=["no_risk_feasible_saving_action"],
            simulations=simulations,
            seed=seed,
            actions=actions,
            forecast_readiness=evidence.readiness,
            evidence_lineage=lineage,
        )
    recommended = _point_winner(
        [
            action
            for action in actions
            if all(
                bound["status"] == "qualified"
                for bound in qualifications[action.action_id].values()
            )
        ]
    )
    if recommended is None:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            reason="mc_probability_indeterminate",
            additional_cases_required=0,
        ).model_copy(update={"actions": actions, "evidence_lineage": lineage})
    action = "ship_candidate" if recommended.candidate_share == 1 else "hybrid_route"
    if recommended.cohort_candidate_shares:
        action = "cohort_route"
    return ProbabilisticMigrationDecision(
        workload=evidence.workload,
        incumbent_model=evidence.incumbent_model,
        candidate_model=evidence.candidate_model,
        verdict="recommend",
        recommended_action=action,
        recommended_candidate_share=recommended.candidate_share,
        recommended_routing=recommended.cohort_candidate_shares,
        reason_codes=["risk_constraints_satisfied"],
        simulations=simulations,
        seed=seed,
        actions=actions,
        forecast_readiness=evidence.readiness,
        evidence_lineage=lineage,
    )


__all__ = [
    "CohortRoutingAction",
    "MigrationActionForecast",
    "MigrationEvidence",
    "MigrationObservation",
    "MigrationRiskPolicy",
    "ForecastCalibrationObservation",
    "ForecastReadiness",
    "MetricForecastReadiness",
    "ProbabilisticMigrationDecision",
    "assess_forecast_readiness",
    "empirical_var_cvar",
    "recommend_model_migration",
]
