"""Seeded regression tests for the sampling-tolerant forecast readiness gate.

The former gate compared empirical interval coverage with the nominal 0.9 as a point,
so a perfectly calibrated forecaster was marked ``calibrated`` in only 8-19% of
assessments (exact binomial, four metrics). These tests pin the repaired behaviour:
a perfect forecaster passes, the manifest's biased and overconfident controls are
flagged, power grows with history, and immaterial-but-certain offsets stay unflagged.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import pytest

from zeroth.econ import probabilistic as subject

METRICS = ("monthly_cost_usd", "success_rate", "p95_latency_ms", "critical_error_rate")
Z90 = NormalDist().inv_cdf(0.95)


def _history(rng, *, periods, relative_sd, mean_shift=0.0, width_factor=1.0, metrics=METRICS):
    rows = []
    for metric in metrics:
        mean = 1000.0 if metric in ("monthly_cost_usd", "p95_latency_ms") else 0.9
        sd = relative_sd * mean
        predicted = mean + mean_shift * mean
        half_width = width_factor * Z90 * sd
        for period in range(periods):
            rows.append(
                subject.ForecastCalibrationObservation(
                    forecast_id=f"{metric}-{period}",
                    metric=metric,
                    predicted_mean=predicted,
                    predicted_low=predicted - half_width,
                    predicted_high=predicted + half_width,
                    observed=rng.gauss(mean, sd),
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=30 * period),
                )
            )
    return rows


def _states(rng, replications, **kwargs):
    states = Counter()
    for _ in range(replications):
        readiness = subject.assess_forecast_readiness(
            _history(rng, **kwargs), required_metrics=set(METRICS)
        )
        states[readiness.calibration_state] += 1
    return states


@pytest.mark.parametrize("periods", [6, 12])
@pytest.mark.parametrize("relative_sd", [0.03, 0.12])
def test_perfectly_calibrated_forecaster_is_calibrated_at_least_ninety_percent(
    periods, relative_sd
):
    rng = random.Random(1_000 * periods + int(relative_sd * 100))
    states = _states(rng, 400, periods=periods, relative_sd=relative_sd)

    # Measured 0.974-0.998 over 3,000 replications; the family-wise budget is 0.05.
    assert states["calibrated"] / 400 >= 0.90


@pytest.mark.parametrize("control", ["biased", "overconfident"])
def test_manifest_controls_are_flagged_at_twelve_periods(control):
    rng = random.Random(7 if control == "biased" else 11)
    overrides = {"mean_shift": 0.25} if control == "biased" else {"width_factor": 0.10}
    states = _states(rng, 200, periods=12, relative_sd=0.12, **overrides)

    assert states["calibrated"] / 200 <= 0.20
    assert states["critical"] / 200 >= 0.80


def test_detection_power_grows_with_history():
    rng = random.Random(23)
    short = _states(rng, 200, periods=6, relative_sd=0.06, width_factor=0.6)
    long = _states(rng, 200, periods=24, relative_sd=0.06, width_factor=0.6)

    assert 1 - long["calibrated"] / 200 > 1 - short["calibrated"] / 200
    assert long["calibrated"] / 200 < 0.25


def test_long_history_does_not_flag_a_statistically_certain_but_immaterial_bias():
    rng = random.Random(5)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=120, relative_sd=0.03, mean_shift=-0.01, metrics=("monthly_cost_usd",)),
    )
    [metric] = readiness.metrics

    assert metric.bias_p_value < readiness.alpha_warning / readiness.family_tests
    assert abs(metric.bias_standard_errors) > 3
    assert abs(metric.relative_bias) < 0.1
    assert metric.calibration_state == "calibrated"


def test_material_and_certain_bias_is_critical():
    rng = random.Random(9)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=12, relative_sd=0.03, mean_shift=0.30, metrics=("monthly_cost_usd",)),
    )
    [metric] = readiness.metrics

    assert metric.bias_p_value < readiness.alpha_critical / readiness.family_tests
    assert metric.coverage_p_value < readiness.alpha_critical / readiness.family_tests
    assert metric.calibration_state == "critical"
    assert readiness.calibration_state == "critical"


def test_family_size_and_alphas_are_reported_and_shared():
    rng = random.Random(3)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=12, relative_sd=0.05), required_metrics=set(METRICS)
    )

    assert readiness.family_tests == 12
    assert readiness.alpha_warning == 0.05
    assert readiness.alpha_critical == 0.01
    assert all(
        row.coverage_p_value is not None
        and row.bias_p_value is not None
        and row.drift_p_value is not None
        for row in readiness.metrics
    )
    assert readiness.interval_coverage is not None
    assert readiness.relative_bias is not None


def test_drift_is_unknown_when_a_half_has_a_single_residual():
    rng = random.Random(4)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=3, relative_sd=0.05, metrics=("monthly_cost_usd",)),
        minimum_periods=2,
    )
    [metric] = readiness.metrics

    assert metric.drift_state == "unknown"
    assert metric.drift_p_value is None
    assert readiness.family_tests == 2


def test_alpha_budget_validation():
    with pytest.raises(ValueError, match="alpha"):
        subject.assess_forecast_readiness([], alpha_warning=0.01, alpha_critical=0.05)
    with pytest.raises(ValueError, match="minimum_interval_coverage"):
        subject.assess_forecast_readiness([], minimum_interval_coverage=1.0)


def test_tail_helpers_match_known_values():
    assert subject._binomial_lower_tail_probability(3, 6, 0.9) == pytest.approx(0.01585, abs=1e-5)
    assert subject._binomial_lower_tail_probability(6, 6, 0.9) == 1.0
    assert subject._student_t_two_sided_p_value(2.015048, 5) == pytest.approx(0.10, abs=1e-5)
    assert subject._student_t_two_sided_p_value(2.228139, 10) == pytest.approx(0.05, abs=1e-5)
    assert subject._student_t_two_sided_p_value(12.7062, 1) == pytest.approx(0.05, abs=1e-5)
    assert subject._student_t_two_sided_p_value(float("inf"), 5) == 0.0
    statistic, p_value = subject._one_sample_t_test([1.0, 1.0, 1.0])
    assert statistic is None and p_value == 0.0
    statistic, p_value = subject._welch_t_test([0.0, 0.0], [0.0, 0.0])
    assert statistic is None and p_value == 1.0


def _sdk_models():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "packaging/sdk/src/zeroth/protocol/models.py"
    spec = importlib.util.spec_from_file_location("readiness_gate_sdk_models", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_readiness_dump_keeps_the_request_wire_schema_of_the_sdk_mirror():
    from tests.econ._forecast_fixtures import evidence

    rng = random.Random(21)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=12, relative_sd=0.05), required_metrics=set(METRICS)
    )
    payload = evidence(count=30, periods=1).model_copy(update={"readiness": readiness}).model_dump()

    assert set(payload["readiness"]) == set(subject.ForecastReadiness().model_dump())
    assert "coverage_p_value" not in payload["readiness"]["metrics"][0]
    world = _sdk_models().MigrationEvidence.model_validate(payload)
    assert world.readiness.calibration_state == readiness.calibration_state
    assert subject.MigrationEvidence.model_validate(payload).readiness.family_tests == 0


def test_readiness_statistics_travel_in_the_decision_lineage():
    from tests.econ._forecast_fixtures import diagnose, evidence, policy

    rng = random.Random(9)
    readiness = subject.assess_forecast_readiness(
        _history(rng, periods=12, relative_sd=0.03, mean_shift=0.30), required_metrics=set(METRICS)
    )
    world = evidence(count=600).model_copy(update={"readiness": readiness})
    report = diagnose(world, policy(), simulations=100)
    tests = report.evidence_lineage["forecast_readiness_tests"]

    assert report.reason_codes == ["forecast_not_calibrated"]
    assert tests["family_tests"] == 12
    assert tests["alpha_warning"] == 0.05 and tests["alpha_critical"] == 0.01
    assert tests["metrics"]["monthly_cost_usd"]["calibration_state"] == "critical"
    assert tests["metrics"]["monthly_cost_usd"]["coverage_p_value"] < 0.01 / 12
    assert set(tests["metrics"]) == set(METRICS)
