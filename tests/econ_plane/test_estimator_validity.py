"""Seeded Monte Carlo checks of the econ-plane estimators against their stated coverage.

Every tolerance below is a stated acceptance threshold from the 2026-09-06 forecast-math
validation, not a snapshot of one run: a test fails only when an estimator's coverage or
false-positive rate leaves the band it claims, so a re-implementation that keeps the
statistical property keeps the test green. Replications are sized so the whole module
runs in well under twenty seconds.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.models import Capability
from zeroth.econ.plane.costing.service import (
    ESTIMATION_INFERRED_WIDTH_UNKNOWN,
    ESTIMATION_MEASURED_SUM,
    ESTIMATION_STUDENT_T_INFERRED,
    OVERHEAD_RATE,
    estimate_cost_for_period,
)
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.counterfactual.service import _pick_interval, run_evaluation
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.statistics.intervals import (
    DRIFT_CRITICAL_SCORE,
    HIGH_VARIANCE_CV,
    MIN_BOOTSTRAP_SAMPLE,
    bootstrap_t_mean_interval,
    coefficient_of_variation,
    drift_state,
    mean_shift_score,
    newcombe_difference_interval,
    ratio_change_interval,
    student_t_mean_interval,
    t_quantile,
    validate_confidence,
    wilson_interval,
    z_quantile,
)
from zeroth.econ.plane.statistics.service import (
    bootstrap_interval,
    confidence_gate,
    hierarchical_interval,
)
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


# --- quantiles -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.80, 1.281552), (0.90, 1.644854), (0.95, 1.959964), (0.99, 2.575829)],
)
def test_z_quantile_is_the_exact_normal_quantile(confidence: float, expected: float) -> None:
    assert z_quantile(confidence) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    ("confidence", "df", "expected"),
    [
        (0.95, 4, 2.776445),
        (0.95, 9, 2.262157),
        (0.95, 29, 2.045230),
        (0.99, 9, 3.249836),
        (0.80, 1, 3.077684),
        (0.95, 1_000_000, 1.959966),
    ],
)
def test_t_quantile_matches_the_reference_tables(confidence: float, df: int, expected: float) -> None:
    assert t_quantile(confidence, df) == pytest.approx(expected, abs=1e-5)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.5, 1.5, float("nan"), float("inf"), True])
def test_confidence_levels_outside_the_open_unit_interval_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        validate_confidence(bad)  # type: ignore[arg-type]


# --- coverage of the proportion and mean intervals ------------------------------------


def _exact_wilson_coverage(n: int, p: float, confidence: float) -> float:
    """Binomial-weighted coverage of the Wilson interval: exact, no sampling noise."""
    return sum(
        math.comb(n, k) * p**k * (1 - p) ** (n - k)
        for k in range(n + 1)
        if wilson_interval(k, n, confidence)[1] <= p <= wilson_interval(k, n, confidence)[2]
    )


@pytest.mark.parametrize("confidence", [0.80, 0.90, 0.95, 0.99])
def test_wilson_coverage_tracks_the_requested_level_at_n_100(confidence: float) -> None:
    # Acceptance: delivered coverage within 0.03 of the requested level at n=100. The old
    # two-value z quantization delivered 0.91 for a requested 0.80 and 0.94-0.96 for 0.99
    # at p=0.5. The exact coverage of a score interval oscillates with p because the
    # binomial is discrete; at p=0.5 plain Wilson is 0.807/0.911/0.943/0.988 for the four
    # levels, and a continuity correction would over-cover by up to 0.10 at 0.80.
    assert abs(_exact_wilson_coverage(100, 0.5, confidence) - confidence) <= 0.03


@pytest.mark.parametrize("confidence", [0.90, 0.95, 0.99])
def test_wilson_coverage_holds_for_rare_positives_at_n_100(confidence: float) -> None:
    assert abs(_exact_wilson_coverage(100, 0.05, confidence) - confidence) <= 0.03


def test_wilson_eighty_percent_dip_at_rare_positives_is_discreteness_not_quantile() -> None:
    # Residual, documented rather than hidden: at requested 0.80 and p=0.05 the exact
    # coverage is 0.754, a property of the discrete binomial at np=5 that no choice of
    # z repairs. It stays above the 0.75 floor and the wider levels are on target.
    assert 0.75 <= _exact_wilson_coverage(100, 0.05, 0.80) < 0.80


@pytest.mark.parametrize("n", [5, 10, 29])
def test_student_t_mean_interval_covers_the_true_mean(n: int) -> None:
    # Acceptance: coverage >= 0.93 at n >= 5 for mu=10, sigma=3 (the shrinkage estimator
    # it replaces covered 0.01-0.21 at these sizes).
    rng = np.random.default_rng(7)
    replications = 3000
    samples = rng.normal(10.0, 3.0, size=(replications, n))
    covered = sum(
        1
        for row in samples
        if (estimate := student_t_mean_interval(row.tolist())).low <= 10.0 <= estimate.high
    )
    assert covered / replications >= 0.93


def test_student_t_mean_interval_has_no_prior_shrinkage() -> None:
    estimate = student_t_mean_interval([10.0, 12.0, 8.0, 11.0, 9.0])
    assert estimate.mean == pytest.approx(10.0)
    assert estimate.defined is True
    assert estimate.method == "student_t"


def test_single_observation_has_an_undefined_width_not_a_fabricated_one() -> None:
    estimate = student_t_mean_interval([42.0])
    assert estimate.defined is False
    assert (estimate.low, estimate.mean, estimate.high) == (42.0, 42.0, 42.0)
    assert hierarchical_interval([42.0], prior_mean=0.0) == (42.0, 42.0, 42.0)


def test_bootstrap_t_covers_a_skewed_mean_from_thirty_observations() -> None:
    # Acceptance: coverage >= 0.90 at n >= 30 on lognormal data. The percentile bootstrap
    # this replaces covered 0.876 at n=30 (and 0.43 at n=2, where it was also used).
    rng = np.random.default_rng(11)
    n, replications = MIN_BOOTSTRAP_SAMPLE, 600
    true_mean = math.exp(2.0 + 0.5)
    samples = rng.lognormal(2.0, 1.0, size=(replications, n))
    covered = sum(
        1
        for row in samples
        if (estimate := bootstrap_t_mean_interval(row.tolist(), iterations=400)).low
        <= true_mean
        <= estimate.high
    )
    assert covered / replications >= 0.90


def test_bootstrap_interval_is_deterministic_for_the_same_input() -> None:
    values = list(np.random.default_rng(3).lognormal(1.0, 0.8, size=40))
    assert bootstrap_interval(values) == bootstrap_interval(values)


def test_bootstrap_interval_falls_back_to_student_t_below_the_bootstrap_floor() -> None:
    values = [1.0, 2.0, 4.0, 8.0, 16.0]
    estimate = student_t_mean_interval(values)
    assert bootstrap_interval(values) == (estimate.mean, estimate.low, estimate.high)


# --- the confidence gate -------------------------------------------------------------


@pytest.mark.parametrize("sample_size", [0, 1, 2, 10, MIN_BOOTSTRAP_SAMPLE - 1])
def test_confidence_gate_never_passes_below_the_sample_floor(sample_size: int) -> None:
    assert confidence_gate(0.95, 0.0, sample_size=sample_size) is False


def test_confidence_gate_passes_a_tight_interval_on_enough_data() -> None:
    assert confidence_gate(0.95, 0.10, sample_size=MIN_BOOTSTRAP_SAMPLE) is True
    assert confidence_gate(0.95, 0.31, sample_size=MIN_BOOTSTRAP_SAMPLE) is False
    assert confidence_gate(0.80, 0.10, sample_size=MIN_BOOTSTRAP_SAMPLE) is False


# --- comparison intervals -------------------------------------------------------------


def test_newcombe_interval_covers_the_true_difference_at_small_n() -> None:
    rng = np.random.default_rng(5)
    n, replications = 10, 4000
    covered = 0
    for _ in range(replications):
        base = int(rng.binomial(n, 0.9))
        cand = int(rng.binomial(n, 0.8))
        _, low, high = newcombe_difference_interval(base, n, cand, n)
        covered += low <= -0.1 <= high
    assert covered / replications >= 0.94


def test_ratio_change_interval_covers_the_true_cost_ratio_at_small_n() -> None:
    rng = np.random.default_rng(9)
    n, replications = 10, 2000
    covered = 0
    for _ in range(replications):
        base_accept = (rng.random(n) < 0.9).astype(float)
        cand_accept = (rng.random(n) < 0.9).astype(float)
        if base_accept.sum() == 0 or cand_accept.sum() == 0:
            continue
        base_cost = np.exp(rng.normal(0.0, 0.5, size=n))
        cand_cost = np.exp(rng.normal(math.log(0.7), 0.5, size=n))
        _, low, high = ratio_change_interval(base_cost, base_accept, cand_cost, cand_accept)
        covered += low <= -0.3 <= high
    assert covered / replications >= 0.92


def test_ratio_change_interval_is_exact_for_constant_costs() -> None:
    change, low, high = ratio_change_interval([1.0] * 10, [1.0] * 10, [1.3] * 10, [1.0] * 10)
    assert change == pytest.approx(0.3)
    assert low == pytest.approx(0.3)
    assert high == pytest.approx(0.3)


# --- the period cost interval ---------------------------------------------------------


def _session(measured: int, inferred: list[float]) -> ScopedSession:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    inner = Session(engine)
    db = ScopedSession(inner, TenantWideScopeContext.for_default_compatibility())
    common = {
        "tenant_id": "default",
        "timestamp": _NOW,
        "capability_id": "cap",
        "implementation_id": "impl",
        "model_version": "m",
        "tool_cost_usd": 0,
        "compute_cost_usd": 0,
        "event_metadata": {},
    }
    for index in range(measured):
        db.add(
            ExecutionEvent(
                execution_id=f"m{index}",
                token_cost_usd=Decimal("0.05"),
                cost_measurement="measured",
                **common,
            )
        )
    for index, cost in enumerate(inferred):
        db.add(
            ExecutionEvent(
                execution_id=f"e{index}",
                token_cost_usd=Decimal(str(cost)),
                cost_measurement="estimated",
                **common,
            )
        )
    db.commit()
    return db


def test_fully_measured_period_has_a_zero_width_interval() -> None:
    # Acceptance: 1,000 measured events -> low == high == total (was [$0, $1,052.50]).
    estimate = estimate_cost_for_period(_session(1000, []), "cap", "impl", _NOW, _NOW)
    total = float(estimate.total_cost_estimate_usd)
    assert total == pytest.approx(1000 * 0.05 * (1 + OVERHEAD_RATE))
    assert float(estimate.cost_interval_low_usd) == pytest.approx(total)
    assert float(estimate.cost_interval_high_usd) == pytest.approx(total)
    assert estimate.estimation_method == ESTIMATION_MEASURED_SUM
    assert estimate.data_quality == "measured"


def test_single_inferred_sample_reports_an_unknown_width() -> None:
    # Acceptance: 1,000 measured + 1 inferred -> bounds at the total, not +-$2,000.
    estimate = estimate_cost_for_period(_session(1000, [5.0]), "cap", "impl", _NOW, _NOW)
    total = float(estimate.total_cost_estimate_usd)
    assert float(estimate.cost_interval_low_usd) == pytest.approx(total)
    assert float(estimate.cost_interval_high_usd) == pytest.approx(total)
    assert estimate.estimation_method == ESTIMATION_INFERRED_WIDTH_UNKNOWN
    assert estimate.data_quality == "mixed"


def test_inferred_subset_width_does_not_scale_with_the_measured_count() -> None:
    inferred = [0.04, 0.05, 0.06, 0.05, 0.04, 0.06]
    few = estimate_cost_for_period(_session(10, inferred), "cap", "impl", _NOW, _NOW)
    many = estimate_cost_for_period(_session(1000, inferred), "cap", "impl", _NOW, _NOW)
    width_few = float(few.cost_interval_high_usd) - float(few.cost_interval_low_usd)
    width_many = float(many.cost_interval_high_usd) - float(many.cost_interval_low_usd)
    expected_half = (
        t_quantile(0.95, len(inferred) - 1)
        * float(np.std(inferred, ddof=1))
        * math.sqrt(len(inferred))
        * (1 + OVERHEAD_RATE)
    )
    assert width_few == pytest.approx(width_many, rel=1e-3)
    assert width_many == pytest.approx(2 * expected_half, rel=1e-3)
    assert many.estimation_method == ESTIMATION_STUDENT_T_INFERRED
    assert float(many.cost_interval_high_usd) - float(many.total_cost_estimate_usd) < 1.0


# --- the counterfactual valuation path -------------------------------------------------


def _outcome(kind: str, value: str, at: datetime | None = None) -> OutcomeEvent:
    return OutcomeEvent(
        tenant_id="tenant-a",
        execution_id="",
        join_key="k",
        capability_id="cap",
        implementation_id="impl",
        outcome_type=kind,
        outcome_value=value,
        outcome_payload_json={},
        occurred_at=at or _NOW,
        ingested_at=at or _NOW,
        outcome_timestamp=at or _NOW,
        provenance="MEASURED",
    )


@pytest.mark.parametrize("p", [0.02, 0.05, 0.2, 0.5])
@pytest.mark.parametrize("n", [20, 50, 200])
def test_binary_dollar_interval_covers_rare_positives(p: float, n: int) -> None:
    # Acceptance: coverage >= 0.90 for every cell and no zero-width intervals. Mapping
    # the Wilson band through observed class means covered 0.27 at p=0.02, n=20 and was
    # zero-width whenever no conversion had been seen (67% of samples there).
    rng = np.random.default_rng(int(p * 1000) + n)
    replications = 800
    covered = zero_width = 0
    truth = n * p * 120.0
    for _ in range(replications):
        positives = int(rng.binomial(n, p))
        outcomes = [_outcome("conversion", "1")] * positives
        outcomes += [_outcome("conversion", "0")] * (n - positives)
        values = [120.0] * positives + [0.0] * (n - positives)
        _method, _estimate, low, high = _pick_interval(values, outcomes, 0.95, "PROXY")
        covered += low <= truth <= high
        zero_width += high - low == 0.0
    assert covered / replications >= 0.90, (p, n, covered / replications)
    assert zero_width == 0


def test_binary_interval_takes_class_values_from_the_formula_parameters() -> None:
    outcomes = [_outcome("conversion", "0")] * 40
    _method, estimate, low, high = _pick_interval(
        [0.0] * 40,
        outcomes,
        0.95,
        "PROXY",
        formula_id="conversion_lift_x_revenue",
        proxy_params={"revenue_per_conversion": 500.0},
    )
    assert estimate == 0.0
    assert low == 0.0
    # Wilson upper bound for 0/40 at 95% is 0.088; the band carries the $500 class.
    assert high == pytest.approx(40 * wilson_interval(0, 40, 0.95)[2] * 500.0)


def test_mixed_binary_outcome_types_do_not_share_one_positive_rate() -> None:
    outcomes = [_outcome("conversion", "1")] * 20 + [_outcome("fraud_flag", "1")] * 20
    method, _estimate, _low, _high = _pick_interval(
        [120.0] * 20 + [-80.0] * 20, outcomes, 0.95, "PROXY"
    )
    assert method == "bootstrap_t"


def test_small_continuous_samples_use_student_t_not_a_bootstrap() -> None:
    values = [10.0, 12.0, 9.0, 11.0, 13.0]
    method, estimate, low, high = _pick_interval(values, [], 0.95, "PROXY")
    interval = student_t_mean_interval(values)
    assert method == "student_t"
    assert estimate == pytest.approx(sum(values))
    assert (low, high) == pytest.approx((interval.low * 5, interval.high * 5))


def test_drift_and_variance_codes_stay_quiet_on_a_stationary_binary_proxy() -> None:
    # Acceptance: on a stationary $120/$0 proxy with p >= 0.3 and n = 100, both
    # P(DRIFT_CRITICAL) and the HIGH_VARIANCE false-positive rate are <= 0.05. The old
    # last-vs-mean score fired critical with probability 1.0 at p = 0.5, and the
    # dollar-denominated pstdev <= 1.0 rule flagged every valuation.
    rng = np.random.default_rng(31)
    replications = 2000
    for p in (0.3, 0.5, 0.9):
        critical = high_variance = 0
        for _ in range(replications):
            values = np.where(rng.random(100) < p, 120.0, 0.0).tolist()
            critical += drift_state(mean_shift_score(values)) == "critical"
            high_variance += coefficient_of_variation(values) > HIGH_VARIANCE_CV
        assert critical / replications <= 0.05, p
        assert high_variance / replications <= 0.05, p


def test_a_level_shift_in_the_recent_window_is_critical_drift() -> None:
    values = [10.0, 11.0, 9.0, 10.5, 9.5] * 16 + [20.0, 21.0, 19.0, 20.5, 19.5] * 4
    assert drift_state(mean_shift_score(values)) == "critical"


def test_drift_score_needs_a_window_to_compare() -> None:
    assert mean_shift_score([5.0, 50.0, 5.0, 50.0]) == 0.0


def test_evaluation_run_request_rejects_a_confidence_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError):
        EvaluationRunRequest(
            capability_id="cap",
            mode="PROXY_MODEL",
            period_start=_NOW,
            period_end=_NOW,
            confidence_level=1.5,
        )
    request = EvaluationRunRequest(
        capability_id="cap", mode="PROXY_MODEL", period_start=_NOW, period_end=_NOW
    )
    assert request.confidence_level == 0.95


def _evaluation_session() -> ScopedSession:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    inner = Session(engine)
    db = ScopedSession(inner, TenantWideScopeContext(tenant_id="tenant-a"))
    db.add(
        Capability(
            id="cap",
            tenant_id="tenant-a",
            name="cap",
            capability_type="REVENUE",
            valuation_config={},
        )
    )
    db.commit()
    return db


def _seed_ordered_shift(db: ScopedSession, *, reversed_insertion: bool) -> None:
    """Sixty $10 outcomes then twenty $20 outcomes, inserted in either time order."""
    rows = []
    for index in range(80):
        at = _NOW + timedelta(minutes=index)
        value = "0.9" if index < 60 else "0.8"  # reopen_rate proxy: (1 - rate) * 100
        rows.append(
            (
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id=f"run-{index}",
                    join_key=f"run-{index}",
                    timestamp=at,
                    capability_id="cap",
                    implementation_id="impl",
                    model_version="m",
                    token_cost_usd=Decimal("0.01"),
                    tool_cost_usd=0,
                    compute_cost_usd=0,
                    cost_measurement="measured",
                    event_metadata={},
                ),
                OutcomeEvent(
                    tenant_id="tenant-a",
                    execution_id="",
                    join_key=f"run-{index}",
                    capability_id="cap",
                    implementation_id="impl",
                    outcome_type="reopen_rate",
                    outcome_value=value,
                    outcome_payload_json={},
                    occurred_at=at,
                    ingested_at=at,
                    outcome_timestamp=at,
                    provenance="MEASURED",
                ),
            )
        )
    if reversed_insertion:
        rows.reverse()
    for execution, outcome in rows:
        db.add(execution)
        db.add(outcome)
    db.commit()


def test_run_evaluation_orders_outcomes_by_time_before_scoring_drift() -> None:
    request = EvaluationRunRequest(
        capability_id="cap",
        mode="PROXY_MODEL",
        period_start=_NOW - timedelta(days=1),
        period_end=_NOW + timedelta(days=1),
    )
    estimates = []
    for reversed_insertion in (False, True):
        db = _evaluation_session()
        _seed_ordered_shift(db, reversed_insertion=reversed_insertion)
        estimates.append(run_evaluation(db, request))
    forward, backward = estimates

    # The level shift is in the most recent fifth of the series whichever order the
    # rows were inserted in; an unordered scan scored whatever row came last.
    assert forward.drift_state == backward.drift_state == "critical"
    assert forward.drift_score > DRIFT_CRITICAL_SCORE
    assert forward.drift_score == pytest.approx(backward.drift_score, rel=1e-9)
    assert forward.confidence_breakdown["variance_ok"] is True
    assert forward.method_metadata["drift_score_units"] == "standard_errors"
    assert forward.interval_method == "bootstrap_t"


def test_run_evaluation_gate_stays_closed_below_thirty_outcomes() -> None:
    db = _evaluation_session()
    for index in range(12):
        at = _NOW + timedelta(minutes=index)
        db.add(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id=f"run-{index}",
                join_key=f"run-{index}",
                timestamp=at,
                capability_id="cap",
                implementation_id="impl",
                model_version="m",
                token_cost_usd=Decimal("0.01"),
                tool_cost_usd=0,
                compute_cost_usd=0,
                cost_measurement="measured",
                event_metadata={},
            )
        )
        db.add(
            OutcomeEvent(
                tenant_id="tenant-a",
                execution_id="",
                join_key=f"run-{index}",
                capability_id="cap",
                implementation_id="impl",
                outcome_type="reopen_rate",
                outcome_value="0.9",
                outcome_payload_json={},
                occurred_at=at,
                ingested_at=at,
                outcome_timestamp=at,
                provenance="MEASURED",
            )
        )
    db.commit()
    request = EvaluationRunRequest(
        capability_id="cap",
        mode="PROXY_MODEL",
        period_start=_NOW - timedelta(days=1),
        period_end=_NOW + timedelta(days=1),
    )

    estimate = run_evaluation(db, request)

    # Twelve identical values: zero relative width, which used to pass the gate.
    assert estimate.relative_interval_width == 0.0
    assert estimate.confidence_gate_passed is False
    assert "LOW_N" in estimate.confidence_breakdown["reason_codes"]
    assert estimate.interval_method == "student_t"
