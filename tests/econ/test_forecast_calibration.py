from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zeroth.econ import probabilistic as subject


def _calibration_observations(*, recent_shift: float = 0.0):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(8):
        observed = 100.0 + (recent_shift if index >= 4 else 0.0)
        rows.append(
            subject.ForecastCalibrationObservation(
                forecast_id=f"forecast-{index}",
                metric="monthly_cost_usd",
                predicted_mean=100.0,
                predicted_low=90.0,
                predicted_high=110.0,
                observed=observed,
                observed_at=start + timedelta(days=30 * index),
            )
        )
    return rows


def test_calibration_assessment_accepts_covered_unbiased_history() -> None:
    readiness = subject.assess_forecast_readiness(
        _calibration_observations(),
        minimum_periods=6,
        minimum_interval_coverage=0.9,
        max_relative_bias=0.1,
        max_relative_residual_shift=0.2,
    )

    assert readiness.calibration_state == "calibrated"
    assert readiness.drift_state == "stable"
    assert readiness.interval_coverage == 1.0
    assert readiness.relative_bias == 0.0
    assert readiness.calibration_periods == 8


def test_calibration_assessment_marks_a_large_recent_residual_shift_critical() -> None:
    readiness = subject.assess_forecast_readiness(
        _calibration_observations(recent_shift=50.0),
        minimum_periods=6,
        minimum_interval_coverage=0.9,
        max_relative_bias=0.1,
        max_relative_residual_shift=0.2,
    )

    assert readiness.calibration_state == "critical"
    assert readiness.drift_state == "critical"
    assert readiness.interval_coverage == 0.5


def test_calibration_assessment_discloses_missing_required_metrics() -> None:
    readiness = subject.assess_forecast_readiness(
        _calibration_observations(),
        required_metrics={"monthly_cost_usd", "success_rate"},
    )

    assert readiness.calibration_state == "unknown"
    assert readiness.missing_metrics == ["success_rate"]
    assert [metric.metric for metric in readiness.metrics] == ["monthly_cost_usd"]


def test_model_migration_abstains_when_calibration_drift_is_critical() -> None:
    readiness = subject.assess_forecast_readiness(_calibration_observations(recent_shift=50.0))
    incumbent = [
        subject.MigrationObservation(
            case_id=f"case-{index}",
            cost_usd=Decimal("1"),
            latency_ms=800,
            accepted=True,
            critical_error=index == 0,
            source="production",
        )
        for index in range(100)
    ]
    candidate = [
        row.model_copy(update={"cost_usd": Decimal("0.5"), "source": "replay"}) for row in incumbent
    ]
    evidence = subject.MigrationEvidence(
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        incumbent=incumbent,
        candidate=candidate,
        period_request_counts=[100],
        demand_horizon="month",
        readiness=readiness,
    )
    policy = subject.MigrationRiskPolicy(
        candidate_shares=[1.0],
        max_critical_error_rate=0.05,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = subject.recommend_model_migration(evidence, policy=policy, simulations=200, seed=7)

    assert report.verdict == "abstain"
    assert report.reason_codes == ["calibration_drift_critical"]
