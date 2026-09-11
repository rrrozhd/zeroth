"""Risk-calibrated model-migration decisions from paired run evidence."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from functools import partial
from itertools import combinations
from math import ceil, comb, exp, fsum, isfinite, isinf, lgamma, log, sqrt
from statistics import NormalDist, fmean, stdev
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

FORECAST_ALGORITHM_VERSION = "nested-paired-monthly-v3-hoeffding99-math1-predictive1"
# Family-wise false-alarm budgets of the readiness gate for a perfectly calibrated
# forecaster; both are split by Bonferroni over every test in the assessment.
_READINESS_ALPHA_WARNING = 0.05
_READINESS_ALPHA_CRITICAL = 0.01
# Demand uncertainty is the empirical law of the history months, so the cost band
# cannot reach beyond the observed demand range. Measured 2026-09-06 against known
# laws with 100 replications per cell (docs/how-to/probabilistic-model-migration.md):
# for 100 or more paired cases the nominal-90% cost band covered 0.61-0.77 of
# realized months with 3 history months (mean 0.69), 0.78-0.88 with 6 (mean 0.83)
# and 0.82-0.92 with 12 (mean 0.87) in the four non-degenerate worlds, so twelve
# is the smallest measured history that meets a 0.85 floor on average.
DEFAULT_MIN_DEMAND_PERIODS = 12
MAX_SIMULATION_WORK = 50_000_000
_BOUNDED_RATE_METRICS = frozenset({"success_rate", "critical_error_rate"})
_CVAR_BATCH_COUNT = 20
_MINIMUM_EFFECTIVE_TAIL_SAMPLES = 100
_MINIMUM_EFFECTIVE_SOURCE_TAIL_SAMPLES = 30
# Evidence requirements are searched up to this many paired cases; beyond it the
# certificate is reported as not reachable rather than as an astronomical count.
_EVIDENCE_REQUIREMENT_SEARCH_CAP = 10_000_000
# E8 private monitor: exact permutation test of one monitoring batch against the
# 24 calibration periods. The smallest attainable p-value is 1/C(24+batch, batch),
# so the batch size and the geometric alpha schedule are chosen together such
# that every batch through the eleventh keeps a non-empty rejection region while
# the per-metric total stays at 0.01/4 = 0.0025 (the series sums exactly to it).
_EXPERIMENTAL_CALIBRATION_PERIODS = 24
_EXPERIMENTAL_MONITORING_BATCH_SIZE = 4
_EXPERIMENTAL_DRIFT_ALPHA_TOTAL = 0.01 / 4
_EXPERIMENTAL_DRIFT_ALPHA_RATIO = 0.8
_EXPERIMENTAL_CALIBRATION_ALPHA = 0.01 / 4
# For 20 independent batches this exceeds the two-sided Student-t critical
# needed for 99% familywise coverage across the policy maximum of 32 tests.
_CVAR_STUDENTIZED_CRITICAL_VALUE = 5.0
_EXPERIMENTAL_METRICS = (
    "monthly_cost_usd",
    "p95_latency_ms",
    "success_rate",
    "critical_error_rate",
)


@dataclass(frozen=True)
class _ExperimentalRiskQualification:
    """Scope and provenance required to run the private risk-law diagnostic."""

    qualification_id: str
    workload: str
    incumbent_model: str
    candidate_model: str
    action_ids: tuple[str, ...]
    metric_supports: tuple[tuple[str, tuple[float, float]], ...]
    loss_support: tuple[float, float]
    currency: str
    loss_formula_version: str
    independent_unit: str
    dependence_kind: str
    artifact_sha256: str
    issuer: str
    valid_from: datetime
    valid_until: datetime
    active: bool


@dataclass(frozen=True)
class _ExperimentalForecastCell:
    """One metric's predicted interval and realized value for a forecast period."""

    metric: str
    predicted_mean: float
    predicted_low: float
    predicted_high: float
    observed: float


@dataclass(frozen=True)
class _ExperimentalCalibrationBundle:
    """A finalized period joining demand and metric forecasts to their outcomes."""

    period_id: str
    forecast_origin_at: datetime
    period_start: datetime
    period_end: datetime
    finalized_at: datetime
    coverage_kind: str
    qualification_id: str
    algorithm_id: str
    action_id: str
    predicted_request_count_mean: float
    predicted_request_count_low: float
    predicted_request_count_high: float
    observed_request_count: int
    cells: tuple[_ExperimentalForecastCell, ...]


@dataclass(frozen=True)
class _ExperimentalDemandFit:
    """Bounded linear demand coefficient retained by the experimental forecaster."""

    metric: str
    coefficient_unclipped: float
    coefficient: float
    intercept: float


@dataclass(frozen=True)
class _ExperimentalMonitoringBatch:
    """One sequential drift-test result for a forecast metric."""

    metric: str
    batch_index: int
    effect: float
    p_value: float
    alpha: float
    critical: bool


@dataclass(frozen=True)
class _ExperimentalReadiness:
    """Private readiness result with demand fits and sequential monitoring evidence."""

    state: Literal["unknown", "calibrated", "critical"]
    reason: str | None
    fit_periods: int
    calibration_periods: int
    demand_fits: tuple[_ExperimentalDemandFit, ...] = ()
    monitoring_batches: tuple[_ExperimentalMonitoringBatch, ...] = ()


@dataclass(frozen=True)
class _ExperimentalSimulationSample:
    """Auditable output from one action draw in the nested simulation."""

    draw_index: int
    action_id: str
    demand: int
    loss: float
    quality_breach: bool
    latency_breach: bool
    critical_error_breach: bool
    monthly_cost_usd: float
    success_rate: float
    p95_latency_ms: float
    critical_error_rate: float
    qualification_id: str


class MigrationObservation(BaseModel):
    """Measured outcome for one incumbent or candidate execution in a paired case."""

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
        """Omit the legacy default when critical-error measurement was not supplied."""
        values = handler(self)
        if "critical_error" not in self.model_fields_set:
            values.pop("critical_error", None)
        return values


class ForecastCalibrationObservation(BaseModel):
    """A historical forecast interval joined to its subsequently observed value."""

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
        """Reject forecast intervals whose lower endpoint exceeds the upper endpoint."""
        if self.predicted_low > self.predicted_high:
            raise ValueError("forecast interval endpoints must be ordered")
        return self


class MetricForecastReadiness(BaseModel):
    """Calibration and drift assessment for one forecasted decision metric."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    calibration_state: Literal["unknown", "calibrated", "warning", "critical"]
    drift_state: Literal["unknown", "stable", "warning", "critical"]
    interval_coverage: float | None = Field(default=None, ge=0, le=1)
    relative_bias: float | None = None
    relative_residual_shift: float | None = Field(default=None, ge=0)
    calibration_periods: int = Field(ge=0)
    assessed_at: datetime | None = None
    # Test statistics behind the states. They are excluded from serialization so the
    # request wire schema stays identical to the SDK mirror (which forbids extras);
    # decisions expose them under ``evidence_lineage["forecast_readiness_tests"]``.
    # Exact one-sided binomial tail of the covered count against the nominal level.
    coverage_p_value: float | None = Field(default=None, ge=0, le=1, exclude=True)
    # Mean residual in units of its standard error, with the two-sided Student-t
    # p-value; None when every residual is identical (no estimable scale).
    bias_standard_errors: float | None = Field(default=None, exclude=True)
    bias_p_value: float | None = Field(default=None, ge=0, le=1, exclude=True)
    # Recent-minus-historical mean residual in Welch standard-error units.
    drift_standard_errors: float | None = Field(default=None, exclude=True)
    drift_p_value: float | None = Field(default=None, ge=0, le=1, exclude=True)


class ForecastReadiness(BaseModel):
    """Aggregate readiness state across every metric required by migration policy."""

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
    # Number of hypothesis tests sharing the family-wise alpha budgets below. Excluded
    # from serialization for the same wire-compatibility reason as the metric statistics.
    family_tests: int = Field(default=0, ge=0, exclude=True)
    alpha_warning: float | None = Field(default=None, gt=0, lt=1, exclude=True)
    alpha_critical: float | None = Field(default=None, gt=0, lt=1, exclude=True)


class MigrationEvidence(BaseModel):
    """Paired execution outcomes, demand history, and readiness used for forecasting."""

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
        """Validate positive demand periods and unambiguous cohort-preserving pairs."""
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
    """Candidate traffic shares for a named, cohort-specific routing alternative."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128)
    cohort_candidate_shares: dict[str, float] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _shares_are_bounded(self) -> CohortRoutingAction:
        """Require finite cohort shares and at least one nonzero candidate route."""
        if any(
            not cohort or not isfinite(share) or share < 0 or share > 1
            for cohort, share in self.cohort_candidate_shares.items()
        ):
            raise ValueError("cohort candidate shares must be between 0 and 1")
        if not any(self.cohort_candidate_shares.values()):
            raise ValueError("routing action must send some traffic to the candidate")
        return self


class MigrationRiskPolicy(BaseModel):
    """Risk limits and candidate routing alternatives for a migration evaluation."""

    model_config = ConfigDict(extra="forbid")

    min_paired_cases: int = Field(default=30, ge=1)
    min_demand_periods: int = Field(default=DEFAULT_MIN_DEMAND_PERIODS, ge=1)
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
        """Keep global shares ordered and all action identifiers unique."""
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
    """Cost, quality, latency, and tail-risk forecast for one routing action."""

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
    # The simultaneous finite-sample Wilson predictive envelope (nominal 90%) that
    # the quality and critical-error certificates are judged against. The rate
    # ``*_p05``/``*_p95`` fields above are the wider of the simulated percentile and
    # this envelope, so they are a conservative band rather than percentiles.
    success_rate_lower_bound: float | None = Field(default=None, ge=0, le=1)
    success_rate_upper_bound: float | None = Field(default=None, ge=0, le=1)
    critical_error_rate_lower_bound: float | None = Field(default=None, ge=0, le=1)
    critical_error_rate_upper_bound: float | None = Field(default=None, ge=0, le=1)
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
    """Fail-closed migration result with forecasts, lineage, and abstention reasons."""

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
    additional_demand_periods_required: int = Field(default=0, ge=0)
    simulations: int
    seed: int
    actions: list[MigrationActionForecast]
    forecast_readiness: ForecastReadiness
    evidence_lineage: dict[str, object] = Field(default_factory=dict)
    decision_id: str | None = None
    evaluated_at: datetime | None = None


def _empirical_rank(size: int, probability: float) -> int:
    """One-based left empirical rank; probability zero selects the minimum.

    Probability means its shortest round-trip decimal spelling, so .07 is
    exactly 7/100, while either nextafter neighbor remains distinct. Integer
    arithmetic avoids multiplication rounding and preserves replication of an
    empirical distribution. Decimal conversion does not use context precision.
    """
    if size <= 0:
        raise ValueError("sample size must be positive")
    if not isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("probability must be finite and between 0 and 1")
    numerator, denominator = Decimal(str(probability)).as_integer_ratio()
    return max(1, (size * numerator + denominator - 1) // denominator)


def empirical_var_cvar(losses: list[float], *, confidence: float) -> tuple[float, float]:
    """Return left empirical VaR and the mean of exactly the worst 1-alpha mass.

    Alpha uses the same decimal-probability convention as all empirical ranks.
    Losses are finite binary floats. Accumulate their exact rational values and
    fractional boundary weight before converting the normalized mean to float.
    This avoids overflowing a finite mean, or losing small cancellation terms
    by scaling/rounding intermediate contributions. Only the final result rounds
    (and may correctly underflow); no epsilon or magnitude cutoff is applied.
    """
    if not losses:
        raise ValueError("losses must not be empty")
    if not isfinite(confidence) or confidence <= 0 or confidence >= 1:
        raise ValueError("confidence must be greater than 0 and less than 1")
    ordered = sorted(float(loss) for loss in losses)
    if not all(isfinite(loss) for loss in ordered):
        raise ValueError("losses must be finite")
    value_at_risk_index = _empirical_rank(len(ordered), confidence) - 1
    # Integrate exactly the declared upper-tail mass, including its boundary atom.
    numerator, denominator = Decimal(str(confidence)).as_integer_ratio()
    tail_numerator = len(ordered) * (denominator - numerator)
    whole_rows, boundary_mass = divmod(tail_numerator, denominator)
    tail_sum = (
        sum(
            (Fraction.from_float(value) for value in ordered[len(ordered) - whole_rows :]),
            Fraction(0),
        )
        * denominator
    )
    if boundary_mass:
        tail_sum += boundary_mass * Fraction.from_float(ordered[len(ordered) - whole_rows - 1])
    return ordered[value_at_risk_index], float(tail_sum / tail_numerator)


def _binomial_lower_tail_probability(successes: int, trials: int, probability: float) -> float:
    """Exact P(X <= successes) for X ~ Binomial(trials, probability)."""
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError("invalid binomial tail counts")
    if not isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("probability must be finite and between 0 and 1")
    tail = fsum(
        comb(trials, count) * probability**count * (1 - probability) ** (trials - count)
        for count in range(successes + 1)
    )
    return min(1.0, tail)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz evaluation of the continued fraction behind the incomplete beta."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b) with the symmetry swap that keeps the continued fraction convergent."""
    if a <= 0 or b <= 0 or not isfinite(x):
        raise ValueError("invalid incomplete beta inputs")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def _student_t_two_sided_p_value(statistic: float, degrees_of_freedom: float) -> float:
    """Two-sided tail probability of Student's t without a SciPy dependency."""
    if not degrees_of_freedom > 0:
        raise ValueError("degrees_of_freedom must be positive")
    if isinf(statistic):
        return 0.0
    x = degrees_of_freedom / (degrees_of_freedom + statistic * statistic)
    return min(1.0, _regularized_incomplete_beta(degrees_of_freedom / 2.0, 0.5, x))


def _one_sample_t_test(values: list[float]) -> tuple[float | None, float]:
    """Return (statistic, p) for H0: mean == 0; a constant sample has no scale."""
    if len(values) < 2:
        raise ValueError("a one-sample t-test needs at least two values")
    mean = fmean(values)
    spread = stdev(values)
    if spread == 0.0:
        return None, (1.0 if mean == 0.0 else 0.0)
    statistic = mean / (spread / sqrt(len(values)))
    return statistic, _student_t_two_sided_p_value(statistic, len(values) - 1)


def _welch_t_test(first: list[float], second: list[float]) -> tuple[float | None, float]:
    """Return (statistic, p) for H0: mean(second) == mean(first) with unequal variances."""
    if len(first) < 2 or len(second) < 2:
        raise ValueError("a Welch t-test needs at least two values per group")
    difference = fmean(second) - fmean(first)
    first_variance = stdev(first) ** 2 / len(first)
    second_variance = stdev(second) ** 2 / len(second)
    pooled = first_variance + second_variance
    if pooled == 0.0:
        return None, (1.0 if difference == 0.0 else 0.0)
    statistic = difference / sqrt(pooled)
    degrees_of_freedom = pooled**2 / (
        (first_variance**2 / (len(first) - 1) if first_variance else 0.0)
        + (second_variance**2 / (len(second) - 1) if second_variance else 0.0)
    )
    return statistic, _student_t_two_sided_p_value(statistic, degrees_of_freedom)


@dataclass(frozen=True)
class _MetricReadinessEvidence:
    """Test statistics for one metric, before family-wise thresholds are applied."""

    metric: str
    periods: int
    coverage: float
    coverage_p_value: float
    relative_bias: float
    bias_standard_errors: float | None
    bias_p_value: float
    relative_residual_shift: float | None
    drift_standard_errors: float | None
    drift_p_value: float | None
    assessed_at: datetime

    @property
    def test_count(self) -> int:
        return 2 + (self.drift_p_value is not None)


def assess_forecast_readiness(
    observations: list[ForecastCalibrationObservation],
    *,
    required_metrics: set[str] | None = None,
    minimum_periods: int = 6,
    minimum_interval_coverage: float = 0.9,
    max_relative_bias: float = 0.1,
    max_relative_residual_shift: float = 0.2,
    alpha_warning: float = _READINESS_ALPHA_WARNING,
    alpha_critical: float = _READINESS_ALPHA_CRITICAL,
) -> ForecastReadiness:
    """Assess interval coverage, bias, and recent residual drift with sampling tolerance.

    Every metric with enough history contributes three tests: an exact one-sided
    binomial test of the covered count against ``minimum_interval_coverage``, a
    one-sample Student-t test of the mean residual, and a Welch t-test of the recent
    half of the residuals against the historical half. The family-wise budgets
    ``alpha_warning`` and ``alpha_critical`` are divided by Bonferroni across all
    tests, so a perfectly calibrated forecaster is flagged in at most ``alpha_warning``
    of assessments regardless of how many metrics or periods it has. The bias and
    drift tests must also clear the legacy materiality floors ``max_relative_bias``
    (warning; twice it for critical) and ``max_relative_residual_shift`` (critical;
    half of it for warning), measured in units of the mean observed magnitude, or in
    absolute probability points for bounded rates, so that long histories do not flag
    immaterial but statistically certain offsets. Coverage carries no floor because a
    rejection already means the band under-covers its nominal level.
    """
    if minimum_periods < 2:
        raise ValueError("minimum_periods must be at least 2")
    if not 0 < minimum_interval_coverage < 1:
        raise ValueError("minimum_interval_coverage must be strictly between 0 and 1")
    if not 0 < alpha_critical <= alpha_warning < 1:
        raise ValueError("alphas must satisfy 0 < alpha_critical <= alpha_warning < 1")
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
    if not by_metric:
        return ForecastReadiness(
            missing_metrics=missing_metrics,
            alpha_warning=alpha_warning,
            alpha_critical=alpha_critical,
        )
    metric_readiness: list[MetricForecastReadiness] = []
    evidence: list[_MetricReadinessEvidence] = []
    for metric, rows in sorted(by_metric.items()):
        ordered = sorted(rows, key=lambda row: row.observed_at)
        if len(ordered) < minimum_periods:
            metric_readiness.append(
                MetricForecastReadiness(
                    metric=metric,
                    calibration_state="unknown",
                    drift_state="unknown",
                    calibration_periods=len(ordered),
                    assessed_at=ordered[-1].observed_at,
                )
            )
            continue
        evidence.append(
            _metric_readiness_evidence(
                metric, ordered, minimum_interval_coverage=minimum_interval_coverage
            )
        )
    family_tests = sum(row.test_count for row in evidence)
    for row in evidence:
        metric_readiness.append(
            _classify_metric_readiness(
                row,
                family_tests=family_tests,
                alpha_warning=alpha_warning,
                alpha_critical=alpha_critical,
                max_relative_bias=max_relative_bias,
                max_relative_residual_shift=max_relative_residual_shift,
            )
        )
    metric_readiness.sort(key=lambda row: row.metric)
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
        family_tests=family_tests,
        alpha_warning=alpha_warning,
        alpha_critical=alpha_critical,
    )


def _metric_readiness_evidence(
    metric: str,
    ordered: list[ForecastCalibrationObservation],
    *,
    minimum_interval_coverage: float,
) -> _MetricReadinessEvidence:
    """Compute coverage, bias and drift statistics for one time-ordered metric."""
    covered = sum(row.predicted_low <= row.observed <= row.predicted_high for row in ordered)
    coverage_p_value = _binomial_lower_tail_probability(
        covered, len(ordered), minimum_interval_coverage
    )
    residuals = [row.observed - row.predicted_mean for row in ordered]
    # These metrics are bounded probabilities, but calibration observations do
    # not carry trial denominators. A binomial standard error therefore cannot
    # be reconstructed. Use probability-point residuals instead of dividing by
    # the observed event rate, which is unstable as a rare-event rate tends to
    # zero. The legacy response field names remain unchanged for compatibility.
    scale = (
        1.0
        if metric in _BOUNDED_RATE_METRICS
        else max(fmean(abs(row.observed) for row in ordered), 1e-9)
    )
    bias_standard_errors, bias_p_value = _one_sample_t_test(residuals)
    split = len(ordered) // 2
    historical, recent = residuals[:split], residuals[split:]
    if len(historical) >= 2 and len(recent) >= 2:
        drift_standard_errors, drift_p_value = _welch_t_test(historical, recent)
        residual_shift = abs(fmean(recent) - fmean(historical)) / scale
    else:
        drift_standard_errors, drift_p_value, residual_shift = None, None, None
    return _MetricReadinessEvidence(
        metric=metric,
        periods=len(ordered),
        coverage=covered / len(ordered),
        coverage_p_value=coverage_p_value,
        relative_bias=fmean(residuals) / scale,
        bias_standard_errors=bias_standard_errors,
        bias_p_value=bias_p_value,
        relative_residual_shift=residual_shift,
        drift_standard_errors=drift_standard_errors,
        drift_p_value=drift_p_value,
        assessed_at=ordered[-1].observed_at,
    )


def _classify_metric_readiness(
    row: _MetricReadinessEvidence,
    *,
    family_tests: int,
    alpha_warning: float,
    alpha_critical: float,
    max_relative_bias: float,
    max_relative_residual_shift: float,
) -> MetricForecastReadiness:
    """Apply Bonferroni-shared alphas and materiality floors to one metric's tests."""
    warning_threshold = alpha_warning / family_tests
    critical_threshold = alpha_critical / family_tests
    bias_magnitude = abs(row.relative_bias)
    if row.drift_p_value is None:
        drift_state = "unknown"
    elif (
        row.drift_p_value < critical_threshold
        and row.relative_residual_shift > max_relative_residual_shift
    ):
        drift_state = "critical"
    elif (
        row.drift_p_value < warning_threshold
        and row.relative_residual_shift > max_relative_residual_shift / 2
    ):
        drift_state = "warning"
    else:
        drift_state = "stable"
    if (
        row.coverage_p_value < critical_threshold
        or (row.bias_p_value < critical_threshold and bias_magnitude > 2 * max_relative_bias)
        or drift_state == "critical"
    ):
        calibration_state = "critical"
    elif (
        row.coverage_p_value < warning_threshold
        or (row.bias_p_value < warning_threshold and bias_magnitude > max_relative_bias)
        or drift_state == "warning"
    ):
        calibration_state = "warning"
    else:
        calibration_state = "calibrated"
    return MetricForecastReadiness(
        metric=row.metric,
        calibration_state=calibration_state,
        drift_state=drift_state,
        interval_coverage=round(row.coverage, 6),
        relative_bias=round(row.relative_bias, 6),
        relative_residual_shift=(
            None if row.relative_residual_shift is None else round(row.relative_residual_shift, 6)
        ),
        calibration_periods=row.periods,
        assessed_at=row.assessed_at,
        coverage_p_value=row.coverage_p_value,
        bias_standard_errors=row.bias_standard_errors,
        bias_p_value=row.bias_p_value,
        drift_standard_errors=row.drift_standard_errors,
        drift_p_value=row.drift_p_value,
    )


def _experimental_drift_alpha(batch_index: int) -> float:
    """Geometric alpha spending for the E8 monitor: 0.0005 * 0.8**(batch_index - 1).

    The series sums exactly to the per-metric budget of 0.01/4 = 0.0025. The former
    harmonic schedule 0.0025/(k(k+1)) fell below the exact permutation floor from
    the third batch, leaving the monitor unable to reject regardless of the shift.
    """
    if type(batch_index) is not int or batch_index < 1:
        raise ValueError("batch_index must be a positive integer")
    return (
        _EXPERIMENTAL_DRIFT_ALPHA_TOTAL
        * (1 - _EXPERIMENTAL_DRIFT_ALPHA_RATIO)
        * _EXPERIMENTAL_DRIFT_ALPHA_RATIO ** (batch_index - 1)
    )


def _experimental_minimum_attainable_p_value() -> float:
    """Smallest p-value the exact batch permutation test can produce."""
    return 1 / comb(
        _EXPERIMENTAL_CALIBRATION_PERIODS + _EXPERIMENTAL_MONITORING_BATCH_SIZE,
        _EXPERIMENTAL_MONITORING_BATCH_SIZE,
    )


def _experimental_drift_batch_attainable(batch_index: int) -> bool:
    """Whether the batch keeps a non-empty rejection region under its alpha."""
    return _experimental_drift_alpha(batch_index) >= _experimental_minimum_attainable_p_value()


def _exact_experimental_permutation_pvalue(
    calibration: list[float], monitoring: list[float]
) -> float:
    """Exact two-group permutation p-value; ties are at least as extreme."""
    calibration_size = _EXPERIMENTAL_CALIBRATION_PERIODS
    batch_size = _EXPERIMENTAL_MONITORING_BATCH_SIZE
    if len(calibration) != calibration_size or len(monitoring) != batch_size:
        raise ValueError(
            f"exact drift test requires {calibration_size} calibration and "
            f"{batch_size} monitoring values"
        )
    values = [*calibration, *monitoring]
    if not all(isfinite(value) for value in values):
        raise ValueError("permutation values must be finite")
    exact_values = [Fraction.from_float(value) for value in values]
    total_sum = sum(exact_values, start=Fraction())
    observed_monitoring_sum = sum(exact_values[calibration_size:], start=Fraction())
    observed = abs(
        (total_sum - observed_monitoring_sum) / calibration_size
        - observed_monitoring_sum / batch_size
    )
    extreme = 0
    total = 0
    for selected in combinations(range(calibration_size + batch_size), batch_size):
        monitoring_sum = sum((exact_values[index] for index in selected), start=Fraction())
        statistic = abs(
            (total_sum - monitoring_sum) / calibration_size - monitoring_sum / batch_size
        )
        extreme += statistic >= observed
        total += 1
    return extreme / total


def _experimental_unknown(reason: str) -> _ExperimentalReadiness:
    """Return a private unknown-readiness result with the supplied reason."""
    return _ExperimentalReadiness(
        state="unknown", reason=reason, fit_periods=12, calibration_periods=24
    )


def _experimental_bundle_contract(
    bundles: list[_ExperimentalCalibrationBundle],
    qualification: _ExperimentalRiskQualification,
) -> str | None:
    if len(bundles) < 36:
        return "demand_calibration_insufficient"
    supports = dict(qualification.metric_supports)
    if set(supports) != set(_EXPERIMENTAL_METRICS):
        return "risk_law_unqualified"
    expected_identity = None
    seen: set[str] = set()
    previous_end = None
    for bundle in bundles:
        identity = (bundle.qualification_id, bundle.algorithm_id, bundle.action_id)
        expected_identity = expected_identity or identity
        if (
            identity != expected_identity
            or bundle.qualification_id != qualification.qualification_id
        ):
            return "risk_qualification_scope_mismatch"
        if bundle.period_id in seen:
            return "demand_calibration_insufficient"
        seen.add(bundle.period_id)
        if (
            bundle.coverage_kind != "census"
            or bundle.forecast_origin_at >= bundle.period_start
            or bundle.period_start >= bundle.period_end
            or bundle.finalized_at < bundle.period_end
            or (previous_end is not None and bundle.period_start < previous_end)
        ):
            return "demand_calibration_insufficient"
        previous_end = bundle.period_end
        if (
            type(bundle.observed_request_count) is not int
            or bundle.observed_request_count <= 0
            or not all(
                isfinite(value)
                for value in (
                    bundle.predicted_request_count_mean,
                    bundle.predicted_request_count_low,
                    bundle.predicted_request_count_high,
                )
            )
            or bundle.predicted_request_count_low > bundle.predicted_request_count_mean
            or bundle.predicted_request_count_mean > bundle.predicted_request_count_high
        ):
            return "demand_calibration_insufficient"
        keyed = {cell.metric: cell for cell in bundle.cells}
        if len(bundle.cells) != 4 or set(keyed) != set(_EXPERIMENTAL_METRICS):
            return "demand_calibration_insufficient"
        for metric, cell in keyed.items():
            support = supports[metric]
            if (
                len(support) != 2
                or not all(isfinite(value) for value in support)
                or support[0] >= support[1]
                or not all(
                    isfinite(value)
                    for value in (
                        cell.predicted_mean,
                        cell.predicted_low,
                        cell.predicted_high,
                        cell.observed,
                    )
                )
                or cell.predicted_low > cell.predicted_high
                or not all(
                    support[0] <= value <= support[1]
                    for value in (
                        cell.predicted_mean,
                        cell.predicted_low,
                        cell.predicted_high,
                        cell.observed,
                    )
                )
            ):
                return "risk_law_unqualified"
    return None


def _assess_experimental_demand_readiness(
    bundles: list[_ExperimentalCalibrationBundle],
    *,
    qualification: _ExperimentalRiskQualification,
) -> _ExperimentalReadiness:
    """Private fixed-epoch demand-conditioned readiness candidate for E8."""
    problem = _experimental_bundle_contract(bundles, qualification)
    if problem:
        return _experimental_unknown(problem)
    fit, calibration, monitoring = bundles[:12], bundles[12:36], bundles[36:]

    def demand_innovation(bundle: _ExperimentalCalibrationBundle) -> float:
        width = max(
            bundle.predicted_request_count_high - bundle.predicted_request_count_low,
            1.0,
        )
        return (bundle.observed_request_count - bundle.predicted_request_count_mean) / width

    fit_demand = [demand_innovation(bundle) for bundle in fit]
    if len(set(fit_demand)) < 6 or max(fit_demand) == min(fit_demand):
        return _experimental_unknown("demand_calibration_insufficient")
    fit_low, fit_high = min(fit_demand), max(fit_demand)
    margin = 0.10 * (fit_high - fit_low)
    if any(
        not fit_low - margin <= demand_innovation(bundle) <= fit_high + margin
        for bundle in monitoring
    ):
        return _experimental_unknown("demand_extrapolation_unqualified")
    if monitoring and len(monitoring) % _EXPERIMENTAL_MONITORING_BATCH_SIZE:
        return _experimental_unknown("monitoring_batch_incomplete")

    supports = dict(qualification.metric_supports)
    demand_mean = fmean(fit_demand)
    denominator = fsum((value - demand_mean) ** 2 for value in fit_demand)
    fits = []
    batches = []
    calibration_adjusted: dict[str, list[float]] = {}
    for metric in _EXPERIMENTAL_METRICS:
        support_low, support_high = supports[metric]
        span = support_high - support_low

        def residual(
            bundle: _ExperimentalCalibrationBundle,
            metric: str = metric,
            span: float = span,
        ) -> float:
            cell = next(cell for cell in bundle.cells if cell.metric == metric)
            return (cell.observed - cell.predicted_mean) / span

        fit_residual = [residual(bundle) for bundle in fit]
        residual_mean = fmean(fit_residual)
        coefficient_unclipped = (
            fsum(
                (demand - demand_mean) * (value - residual_mean)
                for demand, value in zip(fit_demand, fit_residual, strict=True)
            )
            / denominator
        )
        coefficient = max(-1.0, min(1.0, coefficient_unclipped))
        intercept = residual_mean - coefficient * demand_mean
        fits.append(
            _ExperimentalDemandFit(
                metric=metric,
                coefficient_unclipped=coefficient_unclipped,
                coefficient=coefficient,
                intercept=intercept,
            )
        )

        def adjusted(
            bundle: _ExperimentalCalibrationBundle,
            intercept: float = intercept,
            coefficient: float = coefficient,
        ) -> float:
            return residual(bundle) - intercept - coefficient * demand_innovation(bundle)

        values = [adjusted(bundle) for bundle in calibration]
        calibration_adjusted[metric] = values
        covered = sum(
            (cell := next(cell for cell in bundle.cells if cell.metric == metric)).predicted_low
            <= cell.observed
            <= cell.predicted_high
            for bundle in calibration
        )
        if covered / 24 < 0.90 or abs(fmean(values)) > 0.10:
            return _ExperimentalReadiness(
                state="unknown",
                reason="forecast_not_calibrated",
                fit_periods=12,
                calibration_periods=24,
                demand_fits=tuple(fits),
            )

        batch_size = _EXPERIMENTAL_MONITORING_BATCH_SIZE
        for offset in range(0, len(monitoring), batch_size):
            batch_index = offset // batch_size + 1
            recent = [adjusted(bundle) for bundle in monitoring[offset : offset + batch_size]]
            exact_calibration_mean = sum(
                (Fraction.from_float(value) for value in values), start=Fraction()
            ) / len(values)
            exact_monitoring_mean = sum(
                (Fraction.from_float(value) for value in recent), start=Fraction()
            ) / len(recent)
            effect = float(abs(exact_calibration_mean - exact_monitoring_mean))
            p_value = _exact_experimental_permutation_pvalue(values, recent)
            alpha = _experimental_drift_alpha(batch_index)
            batches.append(
                _ExperimentalMonitoringBatch(
                    metric=metric,
                    batch_index=batch_index,
                    effect=effect,
                    p_value=p_value,
                    alpha=alpha,
                    critical=p_value <= alpha and effect > 0.20,
                )
            )

    critical = any(batch.critical for batch in batches)
    return _ExperimentalReadiness(
        state="critical" if critical else "calibrated",
        reason="calibration_drift_critical" if critical else None,
        fit_periods=12,
        calibration_periods=24,
        demand_fits=tuple(fits),
        monitoring_batches=tuple(batches),
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
    index = _empirical_rank(len(ordered), quantile) - 1
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
    additional_demand_periods_required: int = 0,
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
        additional_demand_periods_required=additional_demand_periods_required,
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
        _qualification=None,
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
        reason_codes = diagnostic.reason_codes
        if "risk_law_unqualified" in reason_codes:
            reason_codes = ["experimental_predictive_reliability_unapproved", *reason_codes]
        return diagnostic.model_copy(
            update={"evidence_lineage": lineage, "reason_codes": reason_codes}
        )
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


def _upper_bound_status(lower: float, upper: float, *, limit: float) -> str:
    """Classify a simultaneous interval against an upper-bounded policy limit."""
    if not all(isfinite(value) for value in (lower, upper, limit)) or lower > upper:
        raise ValueError("invalid upper-bound interval")
    if upper <= limit:
        return "qualified"
    if lower > limit:
        return "infeasible"
    return "indeterminate"


def _wilson_score_interval(
    successes: float, *, trials: float, confidence: float
) -> tuple[float, float]:
    """Finite-sample Wilson score interval, including boundary counts.

    ``trials`` may be fractional when an observed count profile is rescaled to ask how
    much more evidence a certificate needs; the algebra is unchanged.

    Zero successes give a lower bound of exactly 0. Computed, that end cancels only to
    within an ulp, and the residue follows the platform's ``inv_cdf`` rounding: the same
    bound was 0.0 on arm64 and ``2**-62`` on x86-64 CI.
    """
    if (
        trials <= 0
        or not isfinite(successes)
        or successes < 0
        or successes > trials
        or not 0 < confidence < 1
    ):
        raise ValueError("invalid Wilson interval inputs")
    probability = successes / trials
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    z_squared = z * z
    denominator = 1 + z_squared / trials
    center = (probability + z_squared / (2 * trials)) / denominator
    half_width = (
        z
        * sqrt(probability * (1 - probability) / trials + z_squared / (4 * trials * trials))
        / denominator
    )
    low = 0.0 if successes == 0 else max(0.0, center - half_width)
    return low, min(1.0, center + half_width)


def _routed_rate_components(
    incumbent: list[MigrationObservation],
    candidate: list[MigrationObservation],
    *,
    share: float,
    cohort_shares: dict[str, float],
    attribute: Literal["accepted", "critical_error"],
) -> list[tuple[float, float, float]]:
    """Literal (weight, successes, trials) arm/cohort components of a routed rate."""
    cohorts = sorted({row.cohort for row in incumbent})
    components = []
    total = len(incumbent)
    for cohort in cohorts:
        incumbent_rows = [row for row in incumbent if row.cohort == cohort]
        candidate_rows = [row for row in candidate if row.cohort == cohort]
        threshold = cohort_shares.get(cohort, 0.0) if cohort_shares else share
        cohort_weight = len(incumbent_rows) / total
        if threshold < 1:
            components.append(
                (
                    cohort_weight * (1 - threshold),
                    sum(bool(getattr(row, attribute)) for row in incumbent_rows),
                    len(incumbent_rows),
                )
            )
        if threshold > 0:
            components.append(
                (
                    cohort_weight * threshold,
                    sum(bool(getattr(row, attribute)) for row in candidate_rows),
                    len(candidate_rows),
                )
            )
    return components


def _predictive_envelope(
    components: list[tuple[float, float, float]],
    *,
    future_trials: int,
    confidence: float,
) -> tuple[float, float]:
    """Bonferroni-chain Wilson source bounds into one future-month predictive envelope."""
    alpha = (1 - confidence) / (len(components) + 1)
    source_low = 0.0
    source_high = 0.0
    for weight, successes, trials in components:
        low, high = _wilson_score_interval(successes, trials=trials, confidence=1 - alpha)
        source_low += weight * low
        source_high += weight * high
    future_low, _ = _wilson_score_interval(
        source_low * future_trials,
        trials=future_trials,
        confidence=1 - alpha,
    )
    _, future_high = _wilson_score_interval(
        source_high * future_trials,
        trials=future_trials,
        confidence=1 - alpha,
    )
    return future_low, future_high


def _predictive_envelope_at_infinite_evidence(
    components: list[tuple[float, float, float]],
    *,
    future_trials: int,
    confidence: float,
) -> tuple[float, float]:
    """The envelope's limit as source evidence grows without bound at fixed proportions.

    Only the future-month step remains, so this is the best any number of additional
    paired cases can achieve for the observed rates and the smallest historical demand.
    """
    alpha = (1 - confidence) / (len(components) + 1)
    proportion = fsum(weight * successes / trials for weight, successes, trials in components)
    future_low, _ = _wilson_score_interval(
        proportion * future_trials, trials=future_trials, confidence=1 - alpha
    )
    _, future_high = _wilson_score_interval(
        proportion * future_trials, trials=future_trials, confidence=1 - alpha
    )
    return future_low, future_high


def _scaled_components(
    components: list[tuple[float, float, float]], factor: float
) -> list[tuple[float, float, float]]:
    """Rescale every count while holding weights and observed proportions fixed."""
    return [
        (weight, successes * factor, trials * factor) for weight, successes, trials in components
    ]


def _routed_rate_predictive_interval(
    incumbent: list[MigrationObservation],
    candidate: list[MigrationObservation],
    *,
    share: float,
    cohort_shares: dict[str, float],
    attribute: Literal["accepted", "critical_error"],
    future_trials: int,
    confidence: float = 0.90,
) -> tuple[float, float]:
    """Compose literal arm/cohort counts into a routed predictive envelope."""
    return _predictive_envelope(
        _routed_rate_components(
            incumbent, candidate, share=share, cohort_shares=cohort_shares, attribute=attribute
        ),
        future_trials=future_trials,
        confidence=confidence,
    )


def _quality_drop_bound_at_scale(
    factor: float,
    *,
    baseline_components: list[tuple[float, float, float]],
    action_components: list[tuple[float, float, float]],
    future_trials: int,
) -> float:
    """Predictive quality-drop upper bound after rescaling every observed count."""
    _, baseline_high = _predictive_envelope(
        _scaled_components(baseline_components, factor),
        future_trials=future_trials,
        confidence=0.90,
    )
    success_low, _ = _predictive_envelope(
        _scaled_components(action_components, factor),
        future_trials=future_trials,
        confidence=0.90,
    )
    return max(0.0, baseline_high - success_low)


def _critical_rate_bound_at_scale(
    factor: float,
    *,
    components: list[tuple[float, float, float]],
    future_trials: int,
) -> float:
    """Predictive critical-error upper bound after rescaling every observed count."""
    _, critical_upper = _predictive_envelope(
        _scaled_components(components, factor), future_trials=future_trials, confidence=0.90
    )
    return critical_upper


def _paired_cases_required(
    bound_at_scale: Callable[[float], float],
    *,
    paired_cases: int,
    limit: float,
    bound_at_infinite_scale: float,
) -> int | None:
    """Smallest paired-case count whose bound meets the limit; None when unreachable.

    ``bound_at_scale(f)`` is the certificate bound after multiplying every observed
    count by ``f`` with proportions held fixed. Wilson bounds tighten monotonically in
    the trial count, so an exponential then binary search over integer case counts
    finds the boundary. Counts above ``_EVIDENCE_REQUIREMENT_SEARCH_CAP`` are reported
    as unreachable rather than as an astronomical number.
    """
    if paired_cases <= 0:
        raise ValueError("paired_cases must be positive")
    if bound_at_scale(1.0) <= limit:
        return paired_cases
    if bound_at_infinite_scale > limit:
        return None
    low = paired_cases
    high = paired_cases
    while bound_at_scale(high / paired_cases) > limit:
        high *= 2
        if high > _EVIDENCE_REQUIREMENT_SEARCH_CAP:
            return None
    while high - low > 1:
        middle = (low + high) // 2
        if bound_at_scale(middle / paired_cases) <= limit:
            high = middle
        else:
            low = middle
    return high


def _simulations_required_for_radius(
    probability: float, *, limit: float, action_count: int, simulations: int
) -> int | None:
    """Simulations after which the Hoeffding radius separates the estimate from the limit.

    Assumes the estimated breach probability persists; None when it equals the limit.
    """
    gap = abs(probability - limit)
    if gap == 0:
        return None
    required = ceil(log(2 * 4 * action_count / 0.01) / (2 * gap * gap))
    return max(0, required - simulations)


def _simulations_required_for_cvar_tail(confidence: float, *, simulations: int) -> int:
    """Additional simulations until the effective simulated tail reaches its minimum."""
    tail_probability = Decimal(1) - Decimal(str(confidence))
    tail_numerator, tail_denominator = tail_probability.as_integer_ratio()
    required = (_MINIMUM_EFFECTIVE_TAIL_SAMPLES - 1) * tail_denominator // tail_numerator + 1
    return max(0, required - simulations)


def _breach_probability_interval(
    probability: float, *, simulations: int, action_count: int, limit: float
) -> dict[str, float | str]:
    """Fixed-N simultaneous numerical bounds, not predictive-risk certification."""
    if simulations <= 0 or action_count <= 0 or not 0 <= probability <= 1 or not 0 <= limit <= 1:
        raise ValueError("invalid probability-bound inputs")
    radius = sqrt(log(2 * 4 * action_count / 0.01) / (2 * simulations))
    lower, upper = max(0.0, probability - radius), min(1.0, probability + radius)
    status = _upper_bound_status(lower, upper, limit=limit)
    return {"lower": lower, "upper": upper, "radius": radius, "status": status}


def _cvar_monte_carlo_interval(
    losses: list[float],
    *,
    confidence: float,
    action_count: int,
    limit: float,
    source_observations: int | None = None,
) -> dict[str, float | int | str | None]:
    """Qualify empirical CVaR with fixed independent simulation batches.

    The 20 batch CVaR estimates preserve draw order and are studentized across
    independent outer simulation draws. A fixed critical value of 5 is
    conservative for 99% familywise coverage over all four constraints and the
    policy maximum of eight actions. At least 100 simulated tail draws and 30
    source-tail units are required; resampling does not create new evidence.
    """
    if (
        len(losses) < _CVAR_BATCH_COUNT
        or action_count <= 0
        or action_count > 8
        or not 0 < confidence < 1
        or (source_observations is not None and source_observations <= 0)
        or not isfinite(limit)
        or not all(isfinite(loss) for loss in losses)
    ):
        raise ValueError("invalid CVaR interval inputs")
    tail_probability = Decimal(1) - Decimal(str(confidence))
    tail_numerator, tail_denominator = tail_probability.as_integer_ratio()
    effective_tail_samples = (
        len(losses) * tail_numerator + tail_denominator - 1
    ) // tail_denominator
    source_observations = source_observations or len(losses)
    effective_source_tail_samples = (
        source_observations * tail_numerator + tail_denominator - 1
    ) // tail_denominator
    required_source_observations = (
        _MINIMUM_EFFECTIVE_SOURCE_TAIL_SAMPLES * tail_denominator + tail_numerator - 1
    ) // tail_numerator
    common = {
        "batch_count": _CVAR_BATCH_COUNT,
        "effective_tail_samples": effective_tail_samples,
        "minimum_effective_tail_samples": _MINIMUM_EFFECTIVE_TAIL_SAMPLES,
        "effective_source_tail_samples": effective_source_tail_samples,
        "minimum_effective_source_tail_samples": _MINIMUM_EFFECTIVE_SOURCE_TAIL_SAMPLES,
        "additional_source_observations_required": max(
            0, required_source_observations - source_observations
        ),
        "critical_value": _CVAR_STUDENTIZED_CRITICAL_VALUE,
    }
    if (
        effective_tail_samples < _MINIMUM_EFFECTIVE_TAIL_SAMPLES
        or effective_source_tail_samples < _MINIMUM_EFFECTIVE_SOURCE_TAIL_SAMPLES
    ):
        return {
            **common,
            "estimate": None,
            "standard_error": None,
            "lower": None,
            "upper": None,
            "status": "insufficient",
        }

    batch_cvars = []
    for batch_index in range(_CVAR_BATCH_COUNT):
        start = batch_index * len(losses) // _CVAR_BATCH_COUNT
        end = (batch_index + 1) * len(losses) // _CVAR_BATCH_COUNT
        _, batch_cvar = empirical_var_cvar(losses[start:end], confidence=confidence)
        batch_cvars.append(batch_cvar)
    estimate = fmean(batch_cvars)
    standard_error = stdev(batch_cvars) / sqrt(_CVAR_BATCH_COUNT)
    half_width = _CVAR_STUDENTIZED_CRITICAL_VALUE * standard_error
    lower, upper = estimate - half_width, estimate + half_width
    status = _upper_bound_status(lower, upper, limit=limit)
    return {
        **common,
        "estimate": estimate,
        "standard_error": standard_error,
        "lower": lower,
        "upper": upper,
        "status": status,
    }


def _histogram_quantile(counts: dict[int, int], total: int, probability: float) -> int:
    """Exact order statistic of integer request counts without expanding demand."""
    cutoff = _empirical_rank(total, probability)
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


def _experimental_action_ids(policy: MigrationRiskPolicy) -> tuple[str, ...]:
    if policy.routing_actions:
        return tuple(action.action_id for action in policy.routing_actions)
    return tuple(f"global-{share:g}" for share in policy.candidate_shares)


def _experimental_qualification_reason(
    qualification: _ExperimentalRiskQualification | None,
    evidence: MigrationEvidence,
    policy: MigrationRiskPolicy,
    checked_at: datetime,
) -> str | None:
    if qualification is None or not isinstance(qualification, _ExperimentalRiskQualification):
        return "risk_law_unqualified"
    if not qualification.active:
        return "risk_law_unqualified"
    if (
        checked_at.tzinfo is None
        or qualification.valid_from.tzinfo is None
        or qualification.valid_until.tzinfo is None
    ):
        return "risk_law_unqualified"
    if not qualification.valid_from <= checked_at <= qualification.valid_until:
        return "risk_law_unqualified"
    if (
        qualification.workload != evidence.workload
        or qualification.incumbent_model != evidence.incumbent_model
        or qualification.candidate_model != evidence.candidate_model
        or qualification.action_ids != _experimental_action_ids(policy)
        or qualification.currency != "USD"
        or qualification.loss_formula_version != "incremental-cost-critical-penalty-v1"
        or qualification.independent_unit != "paired-request-and-independent-month"
    ):
        return "risk_qualification_scope_mismatch"
    if qualification.dependence_kind != "independent_paired_requests_and_periods":
        return "dependence_unqualified"
    if (
        len(qualification.artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in qualification.artifact_sha256)
        or not qualification.issuer
    ):
        return "risk_law_unqualified"
    supports = dict(qualification.metric_supports)
    if len(qualification.metric_supports) != 4 or set(supports) != set(_EXPERIMENTAL_METRICS):
        return "loss_support_unqualified"
    if any(
        len(bounds) != 2 or not all(isfinite(value) for value in bounds) or bounds[0] >= bounds[1]
        for bounds in supports.values()
    ):
        return "loss_support_unqualified"
    loss_support = qualification.loss_support
    if (
        len(loss_support) != 2
        or not all(isfinite(value) for value in loss_support)
        or loss_support[0] >= loss_support[1]
    ):
        return "loss_support_unqualified"
    return None


def _readiness_test_lineage(readiness: ForecastReadiness) -> dict[str, object]:
    """Wire-safe copy of the readiness test statistics that the request schema excludes."""
    return {
        "family_tests": readiness.family_tests,
        "alpha_warning": readiness.alpha_warning,
        "alpha_critical": readiness.alpha_critical,
        "metrics": {
            row.metric: {
                "calibration_state": row.calibration_state,
                "drift_state": row.drift_state,
                "coverage_p_value": row.coverage_p_value,
                "bias_standard_errors": row.bias_standard_errors,
                "bias_p_value": row.bias_p_value,
                "drift_standard_errors": row.drift_standard_errors,
                "drift_p_value": row.drift_p_value,
            }
            for row in readiness.metrics
        },
    }


def _action_case_requirement(qualification: dict[str, dict]) -> int | None:
    """Additional paired cases for one action's evidence-bound certificates; None if unreachable."""
    parts = []
    for metric in ("quality", "critical_error"):
        required = qualification[metric]["additional_cases_required"]
        if required is None:
            return None
        parts.append(int(required))
    parts.append(int(qualification["cvar"]["additional_source_observations_required"]))
    return max(parts)


def _diagnose_model_migration(
    evidence: MigrationEvidence,
    *,
    policy: MigrationRiskPolicy,
    simulations: int = 10_000,
    seed: int = 7,
    _qualification: _ExperimentalRiskQualification | None = None,
    _qualification_checked_at: datetime | None = None,
    _observer: Callable[[_ExperimentalSimulationSample], None] | None = None,
) -> ProbabilisticMigrationDecision:
    """Internal mathematical diagnostic; not a public migration authorization."""
    if simulations < 100:
        raise ValueError("simulations must be at least 100")
    demand_periods = len(evidence.period_request_counts)
    demand_shortfall = max(0, policy.min_demand_periods - demand_periods)
    if evidence.demand_horizon != "month":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
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
            additional_demand_periods_required=demand_shortfall,
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
            additional_demand_periods_required=demand_shortfall,
            reason="critical_error_measurement_missing",
            additional_cases_required=0,
        )
    readiness = evidence.readiness
    readiness_tests = _readiness_test_lineage(readiness)
    if readiness.drift_state == "critical":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
            reason="calibration_drift_critical",
            additional_cases_required=0,
        ).model_copy(
            update={"evidence_lineage": {"forecast_readiness_tests": readiness_tests}}
        )
    if readiness.drift_state == "warning" and not policy.allow_drift_warning:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
            reason="calibration_drift_warning",
            additional_cases_required=0,
        ).model_copy(
            update={"evidence_lineage": {"forecast_readiness_tests": readiness_tests}}
        )
    if policy.require_calibrated_forecast and readiness.calibration_state != "calibrated":
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
            reason="forecast_not_calibrated",
            additional_cases_required=0,
        ).model_copy(
            update={"evidence_lineage": {"forecast_readiness_tests": readiness_tests}}
        )
    incumbent, candidate = _paired_observations(evidence)
    paired_cases = len(incumbent)
    if paired_cases < policy.min_paired_cases:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
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
            additional_demand_periods_required=demand_shortfall,
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
    qualification_reason = _experimental_qualification_reason(
        _qualification,
        evidence,
        policy,
        _qualification_checked_at or datetime.now(UTC),
    )
    if qualification_reason is not None:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
            reason=qualification_reason,
            additional_cases_required=0,
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
    for draw_index in range(simulations):
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
            if _observer is not None:
                assert isinstance(_qualification, _ExperimentalRiskQualification)
                _observer(
                    _ExperimentalSimulationSample(
                        draw_index=draw_index,
                        action_id=action_id,
                        demand=demand,
                        loss=loss,
                        quality_breach=quality_drop > policy.max_quality_drop,
                        latency_breach=action_p95_latency > policy.max_p95_latency_ms,
                        critical_error_breach=(
                            action_critical_rate > policy.max_critical_error_rate
                        ),
                        monthly_cost_usd=action_cost,
                        success_rate=action_success_rate,
                        p95_latency_ms=float(action_p95_latency),
                        critical_error_rate=action_critical_rate,
                        qualification_id=_qualification.qualification_id,
                    )
                )

    predictive_rate_intervals = {}
    actions: list[MigrationActionForecast] = []
    for action_id, share, cohort_shares in routing_specs:
        values = scenario_results[action_id]
        probability_quality_breach = fmean(values["quality_breach"])
        probability_latency_breach = fmean(values["latency_breach"])
        probability_critical_breach = fmean(values["critical_breach"])
        value_at_risk, cvar = empirical_var_cvar(values["loss"], confidence=policy.cvar_confidence)
        future_trials = min(evidence.period_request_counts)
        success_rate_low, success_rate_high = _routed_rate_predictive_interval(
            incumbent,
            candidate,
            share=share,
            cohort_shares=cohort_shares,
            attribute="accepted",
            future_trials=future_trials,
        )
        critical_rate_low, critical_rate_high = _routed_rate_predictive_interval(
            incumbent,
            candidate,
            share=share,
            cohort_shares=cohort_shares,
            attribute="critical_error",
            future_trials=future_trials,
        )
        predictive_rate_intervals[action_id] = {
            "success": (success_rate_low, success_rate_high),
            "critical_error": (critical_rate_low, critical_rate_high),
        }
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
                success_rate_p05=min(
                    _empirical_quantile(values["success_rate"], 0.05), success_rate_low
                ),
                success_rate_p95=max(
                    _empirical_quantile(values["success_rate"], 0.95), success_rate_high
                ),
                expected_p95_latency_ms=fmean(values["p95_latency"]),
                p95_latency_p05_ms=_empirical_quantile(values["p95_latency"], 0.05),
                p95_latency_p95_ms=_empirical_quantile(values["p95_latency"], 0.95),
                expected_critical_error_rate=fmean(values["critical_rate"]),
                critical_error_rate_p05=min(
                    _empirical_quantile(values["critical_rate"], 0.05), critical_rate_low
                ),
                critical_error_rate_p95=max(
                    _empirical_quantile(values["critical_rate"], 0.95), critical_rate_high
                ),
                success_rate_lower_bound=success_rate_low,
                success_rate_upper_bound=success_rate_high,
                critical_error_rate_lower_bound=critical_rate_low,
                critical_error_rate_upper_bound=critical_rate_high,
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

    _, baseline_success_high = _routed_rate_predictive_interval(
        incumbent,
        candidate,
        share=0.0,
        cohort_shares={},
        attribute="accepted",
        future_trials=min(evidence.period_request_counts),
    )
    future_trials = min(evidence.period_request_counts)
    baseline_components = _routed_rate_components(
        incumbent, candidate, share=0.0, cohort_shares={}, attribute="accepted"
    )
    qualifications = {}
    for action in actions:
        action_qualification = {
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
        success_low, success_high = predictive_rate_intervals[action.action_id]["success"]
        critical_low, critical_high = predictive_rate_intervals[action.action_id]["critical_error"]
        action_qualification["quality"].update(
            {
                "predictive_success_rate_lower": success_low,
                "predictive_success_rate_upper": success_high,
                "predictive_quality_drop_upper": max(0.0, baseline_success_high - success_low),
            }
        )
        quality_envelope_qualified = (
            action_qualification["quality"]["predictive_quality_drop_upper"]
            <= policy.max_quality_drop
        )
        if action_qualification["quality"]["status"] == "qualified" and not (
            quality_envelope_qualified
        ):
            action_qualification["quality"]["status"] = "indeterminate"
        action_qualification["critical_error"].update(
            {
                "predictive_rate_lower": critical_low,
                "predictive_rate_upper": critical_high,
            }
        )
        critical_envelope_qualified = critical_high <= policy.max_critical_error_rate
        if action_qualification["critical_error"]["status"] == "qualified" and not (
            critical_envelope_qualified
        ):
            action_qualification["critical_error"]["status"] = "indeterminate"
        action_qualification["cvar"] = _cvar_monte_carlo_interval(
            scenario_results[action.action_id]["loss"],
            confidence=policy.cvar_confidence,
            action_count=len(actions),
            limit=float(policy.max_cvar_loss_usd),
            source_observations=paired_cases,
        )
        # Evidence requirements: how much more of what would let each certificate
        # qualify, assuming the observed rates, cohort mix and demand persist.
        for metric, probability in (
            ("quality", action.probability_quality_breach),
            ("latency", action.probability_latency_breach),
            ("critical_error", action.probability_critical_error_breach),
        ):
            certificate = action_qualification[metric]
            certificate["monte_carlo_status"] = _upper_bound_status(
                certificate["lower"],
                certificate["upper"],
                limit=policy.max_constraint_breach_probability,
            )
            certificate["additional_simulations_required"] = (
                _simulations_required_for_radius(
                    probability,
                    limit=policy.max_constraint_breach_probability,
                    action_count=len(actions),
                    simulations=simulations,
                )
                if certificate["monte_carlo_status"] == "indeterminate"
                else 0
            )
        action_components = {
            attribute: _routed_rate_components(
                incumbent,
                candidate,
                share=action.candidate_share,
                cohort_shares=action.cohort_candidate_shares,
                attribute=attribute,
            )
            for attribute in ("accepted", "critical_error")
        }
        quality_drop_bound = partial(
            _quality_drop_bound_at_scale,
            baseline_components=baseline_components,
            action_components=action_components["accepted"],
            future_trials=future_trials,
        )
        critical_rate_bound = partial(
            _critical_rate_bound_at_scale,
            components=action_components["critical_error"],
            future_trials=future_trials,
        )
        _, baseline_high_limit = _predictive_envelope_at_infinite_evidence(
            baseline_components, future_trials=future_trials, confidence=0.90
        )
        success_low_limit, _ = _predictive_envelope_at_infinite_evidence(
            action_components["accepted"], future_trials=future_trials, confidence=0.90
        )
        _, critical_high_limit = _predictive_envelope_at_infinite_evidence(
            action_components["critical_error"], future_trials=future_trials, confidence=0.90
        )
        quality_cases = _paired_cases_required(
            quality_drop_bound,
            paired_cases=paired_cases,
            limit=policy.max_quality_drop,
            bound_at_infinite_scale=max(0.0, baseline_high_limit - success_low_limit),
        )
        critical_cases = _paired_cases_required(
            critical_rate_bound,
            paired_cases=paired_cases,
            limit=policy.max_critical_error_rate,
            bound_at_infinite_scale=critical_high_limit,
        )
        action_qualification["quality"].update(
            {
                "envelope_status": "qualified" if quality_envelope_qualified else "indeterminate",
                "additional_cases_required": (
                    None if quality_cases is None else quality_cases - paired_cases
                ),
                "reachable_by_additional_cases": quality_cases is not None,
            }
        )
        action_qualification["critical_error"].update(
            {
                "envelope_status": (
                    "qualified" if critical_envelope_qualified else "indeterminate"
                ),
                "additional_cases_required": (
                    None if critical_cases is None else critical_cases - paired_cases
                ),
                "reachable_by_additional_cases": critical_cases is not None,
            }
        )
        action_qualification["cvar"]["additional_simulations_required"] = (
            _simulations_required_for_cvar_tail(policy.cvar_confidence, simulations=simulations)
        )
        qualifications[action.action_id] = action_qualification
    lineage = {
        "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
        "forecast_readiness_tests": readiness_tests,
        "planned_simulation_work": planned_work,
        "simulation_work_limit": MAX_SIMULATION_WORK,
        "numerical_qualification": {
            "method": "fixed_N_Hoeffding_and_fixed_20_batch_studentized_CVaR",
            "familywise_confidence": 0.99,
            "metric_action_comparisons": 4 * len(actions),
            "simulations": simulations,
            "actions": qualifications,
            "scope": (
                "per-metric chance and CVaR Monte Carlo error; excludes model risk "
                "and joint any-breach risk"
            ),
            "evidence_requirements_assume": (
                "observed rates, cohort mix and the smallest historical demand persist"
            ),
        },
        "demand_history": {
            "periods": demand_periods,
            "minimum_periods": policy.min_demand_periods,
            "additional_periods_required": demand_shortfall,
            "law": "empirical distribution of the observed period request counts",
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
    reason_codes: list[str] = []
    additional_cases_required = 0
    if recommended is None:
        saving_actions = [
            action
            for action in actions
            if action.feasible and action.expected_monthly_savings_usd > 0
        ]
        chance_indeterminate = any(
            any(
                qualifications[action.action_id][metric]["status"] != "qualified"
                for metric in ("quality", "latency", "critical_error")
            )
            for action in saving_actions
        )
        reason_codes.append(
            "mc_probability_indeterminate" if chance_indeterminate else "mc_cvar_indeterminate"
        )
        # Fewest additional paired cases after which some saving action's quality,
        # critical-error and CVaR certificates can all qualify; per-action detail
        # is in the lineage. None means no finite case count reaches the limit.
        requirements = [
            _action_case_requirement(qualifications[action.action_id])
            for action in saving_actions
        ]
        finite = [value for value in requirements if value is not None]
        if finite:
            additional_cases_required = min(finite)
        elif requirements:
            reason_codes.append("evidence_requirement_unreachable")
    if demand_shortfall:
        reason_codes.append("demand_history_insufficient")
    if reason_codes:
        return _abstention(
            evidence,
            policy=policy,
            simulations=simulations,
            seed=seed,
            additional_demand_periods_required=demand_shortfall,
            reason=reason_codes[0],
            additional_cases_required=additional_cases_required,
        ).model_copy(
            update={"actions": actions, "evidence_lineage": lineage, "reason_codes": reason_codes}
        )
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
