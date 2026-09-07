from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from math import comb

import pytest

import zeroth.econ.probabilistic as subject

NOW = datetime(2026, 9, 3, tzinfo=UTC)
METRICS = (
    "monthly_cost_usd",
    "p95_latency_ms",
    "success_rate",
    "critical_error_rate",
)
SUPPORTS = (
    ("monthly_cost_usd", (0.0, 320.0)),
    ("p95_latency_ms", (0.0, 3600.0)),
    ("success_rate", (0.0, 1.0)),
    ("critical_error_rate", (0.0, 1.0)),
)
MEANS = {
    "monthly_cost_usd": 120.0,
    "p95_latency_ms": 1200.0,
    "success_rate": 0.25,
    "critical_error_rate": 0.3,
}


def qualification(*, active=True, dependence="independent_paired_requests_and_periods"):
    return subject._ExperimentalRiskQualification(
        qualification_id="qual-e8-001",
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        action_ids=("global-1",),
        metric_supports=SUPPORTS,
        loss_support=(-10_000.0, 10_000.0),
        currency="USD",
        loss_formula_version="incremental-cost-critical-penalty-v1",
        independent_unit="paired-request-and-independent-month",
        dependence_kind=dependence,
        artifact_sha256="a" * 64,
        issuer="independent-e8",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        active=active,
    )


def bundle(index, *, shift=0.0, observed_demand=None, demand_slope=0.08):
    fit_x = (-5 + index % 11) / 10
    demand = int(100 + 20 * fit_x) if observed_demand is None else observed_demand
    cells = []
    for metric, (low, high) in SUPPORTS:
        residual = demand_slope * fit_x + shift
        observed = MEANS[metric] + residual * (high - low)
        cells.append(
            subject._ExperimentalForecastCell(
                metric=metric,
                predicted_mean=MEANS[metric],
                predicted_low=low,
                predicted_high=high,
                observed=observed,
            )
        )
    start = NOW + timedelta(days=31 * index)
    return subject._ExperimentalCalibrationBundle(
        period_id=f"period-{index:03}",
        forecast_origin_at=start - timedelta(days=1),
        period_start=start,
        period_end=start + timedelta(days=30),
        finalized_at=start + timedelta(days=31),
        coverage_kind="census",
        qualification_id="qual-e8-001",
        algorithm_id="experimental-demand-conditioned-v1",
        action_id="global-1",
        predicted_request_count_mean=100.0,
        predicted_request_count_low=90.0,
        predicted_request_count_high=110.0,
        observed_request_count=demand,
        cells=tuple(cells),
    )


def evidence():
    def observations(model, cost):
        return [
            subject.MigrationObservation(
                case_id=f"case-{index}",
                cost_usd=Decimal(cost),
                latency_ms=800,
                accepted=True,
                critical_error=index == 0,
                source="e8-test",
            )
            for index in range(100)
        ]

    return subject.MigrationEvidence(
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        incumbent=observations("model-a", "1.00"),
        candidate=observations("model-b", "0.50"),
        period_request_counts=[90, 100, 110],
        demand_horizon="month",
        readiness=subject.ForecastReadiness(
            calibration_state="calibrated",
            drift_state="stable",
            interval_coverage=0.95,
            calibration_periods=36,
        ),
    )


def policy():
    return subject.MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.02,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("100"),
    )


def test_readiness_requires_exact_fit_and_fixed_calibration_windows():
    insufficient = subject._assess_experimental_demand_readiness(
        [bundle(index) for index in range(35)], qualification=qualification()
    )
    ready = subject._assess_experimental_demand_readiness(
        [bundle(index) for index in range(36)], qualification=qualification()
    )

    assert insufficient.state == "unknown"
    assert insufficient.reason == "demand_calibration_insufficient"
    assert ready.state == "calibrated"
    assert ready.fit_periods == 12
    assert ready.calibration_periods == 24


@pytest.mark.parametrize("abrupt_shift", [0.25, 0.30])
def test_monitor_uses_exact_permutation_ties_alpha_and_strict_effect_boundary(
    abrupt_shift,
):
    assert subject._exact_experimental_permutation_pvalue([0.0] * 24, [0.0] * 4) == 1.0
    expected = 1 / comb(28, 4)
    assert subject._exact_experimental_permutation_pvalue([0.0] * 24, [0.25] * 4) == expected
    assert subject._experimental_drift_alpha(1) == pytest.approx(0.0005)

    boundary = subject._assess_experimental_demand_readiness(
        [
            *[bundle(index, demand_slope=0.0) for index in range(36)],
            *[bundle(index, shift=0.20, demand_slope=0.0) for index in range(36, 40)],
        ],
        qualification=qualification(),
    )
    detected = subject._assess_experimental_demand_readiness(
        [
            *[bundle(index, demand_slope=0.0) for index in range(36)],
            *[bundle(index, shift=abrupt_shift, demand_slope=0.0) for index in range(36, 40)],
        ],
        qualification=qualification(),
    )

    assert boundary.state == "calibrated"
    assert boundary.monitoring_batches[0].effect == pytest.approx(0.20)
    assert detected.state == "critical"
    assert detected.reason == "calibration_drift_critical"
    assert detected.monitoring_batches[0].p_value <= detected.monitoring_batches[0].alpha
    assert detected.monitoring_batches[0].effect > 0.20


def test_monitor_spends_alpha_by_disjoint_batch_without_refitting():
    assessed = subject._assess_experimental_demand_readiness(
        [
            *[bundle(index) for index in range(36)],
            *[bundle(index) for index in range(36, 40)],
            *[bundle(index, shift=0.25) for index in range(40, 44)],
        ],
        qualification=qualification(),
    )
    cost_batches = [
        batch for batch in assessed.monitoring_batches if batch.metric == "monthly_cost_usd"
    ]

    assert [batch.batch_index for batch in cost_batches] == [1, 2]
    assert cost_batches[0].alpha == pytest.approx(subject._experimental_drift_alpha(1))
    assert cost_batches[1].alpha == pytest.approx(subject._experimental_drift_alpha(2))
    assert cost_batches[1].critical
    assert (
        assessed.demand_fits[0]
        == subject._assess_experimental_demand_readiness(
            [bundle(index) for index in range(36)], qualification=qualification()
        ).demand_fits[0]
    )


def test_permutation_does_not_turn_nearby_floats_into_ties():
    calibration = [0.0] * 24
    monitoring = [1e-16] * 4
    assert subject._exact_experimental_permutation_pvalue(calibration, monitoring) == float(
        Fraction(1, comb(28, 4))
    )


def test_demand_sufficiency_and_extrapolation_fail_closed():
    zero_range = [bundle(index, observed_demand=100) for index in range(36)]
    extrapolated = [
        *[bundle(index) for index in range(36)],
        *[bundle(index, observed_demand=150) for index in range(36, 40)],
    ]

    assert (
        subject._assess_experimental_demand_readiness(
            zero_range, qualification=qualification()
        ).reason
        == "demand_calibration_insufficient"
    )
    assert (
        subject._assess_experimental_demand_readiness(
            extrapolated, qualification=qualification()
        ).reason
        == "demand_extrapolation_unqualified"
    )


def test_forecast_mean_may_be_above_p95_when_each_value_is_in_support():
    bundles = []
    for index in range(36):
        original = bundle(index)
        cells = tuple(
            replace(
                cell,
                predicted_mean=200.0,
                predicted_low=100.0,
                predicted_high=150.0,
            )
            if cell.metric == "monthly_cost_usd"
            else cell
            for cell in original.cells
        )
        bundles.append(replace(original, cells=cells))

    assessed = subject._assess_experimental_demand_readiness(bundles, qualification=qualification())

    assert assessed.state == "calibrated"
    assert assessed.reason is None


def test_unqualified_public_call_abstains_before_rng(monkeypatch):
    def forbidden_random(_seed):
        raise AssertionError("RNG initialized before qualification")

    monkeypatch.setattr(subject.random, "Random", forbidden_random)
    result = subject.recommend_model_migration(evidence(), policy=policy(), simulations=100, seed=7)

    assert result.verdict == "abstain"
    assert result.actions == []
    assert "risk_law_unqualified" in result.reason_codes
    assert "qualification" not in subject.MigrationEvidence.model_fields


def test_omitted_private_qualification_abstains_before_rng(monkeypatch):
    def forbidden_random(_seed):
        raise AssertionError("RNG initialized without an explicit qualification")

    monkeypatch.setattr(subject.random, "Random", forbidden_random)
    result = subject._diagnose_model_migration(evidence(), policy=policy(), simulations=100, seed=7)

    assert result.verdict == "abstain"
    assert result.actions == []
    assert result.reason_codes == ["risk_law_unqualified"]


@pytest.mark.parametrize(
    ("qualification_kwargs", "reason"),
    [
        ({"active": False}, "risk_law_unqualified"),
        ({"dependence": "unqualified"}, "dependence_unqualified"),
    ],
)
def test_invalid_private_qualification_abstains_before_rng(
    monkeypatch, qualification_kwargs, reason
):
    def forbidden_random(_seed):
        raise AssertionError("RNG initialized before qualification")

    monkeypatch.setattr(subject.random, "Random", forbidden_random)
    result = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=7,
        _qualification=qualification(**qualification_kwargs),
        _qualification_checked_at=NOW,
    )
    assert result.reason_codes == [reason]


@pytest.mark.parametrize(
    ("qualified", "reason"),
    [
        (
            replace(qualification(), valid_until=NOW - timedelta(seconds=1)),
            "risk_law_unqualified",
        ),
        (
            replace(qualification(), workload="different-workload"),
            "risk_qualification_scope_mismatch",
        ),
        (
            replace(qualification(), loss_support=(-float("inf"), 10_000.0)),
            "loss_support_unqualified",
        ),
    ],
)
def test_expired_wrong_scope_and_unbounded_qualification_stop_before_rng(
    monkeypatch, qualified, reason
):
    def forbidden_random(_seed):
        raise AssertionError("RNG initialized before qualification")

    monkeypatch.setattr(subject.random, "Random", forbidden_random)
    result = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=7,
        _qualification=qualified,
        _qualification_checked_at=NOW,
    )

    assert result.reason_codes == [reason]


def test_recording_observer_is_complete_immutable_and_behavior_preserving():
    records = []
    qualified = qualification()
    plain = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=11,
        _qualification=qualified,
        _qualification_checked_at=NOW,
    )
    observed = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=11,
        _qualification=qualified,
        _qualification_checked_at=NOW,
        _observer=records.append,
    )

    assert observed == plain
    assert len(records) == 100
    assert [record.draw_index for record in records] == list(range(100))
    assert {record.action_id for record in records} == {"global-1"}
    assert all(record.qualification_id == qualified.qualification_id for record in records)
    with pytest.raises(FrozenInstanceError):
        records[0].loss = 0


def test_recording_observer_preserves_subsequent_rng_state(monkeypatch):
    real_random = subject.random.Random
    created = []

    def recording_random(seed):
        instance = real_random(seed)
        created.append(instance)
        return instance

    monkeypatch.setattr(subject.random, "Random", recording_random)
    qualified = qualification()
    plain = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=11,
        _qualification=qualified,
        _qualification_checked_at=NOW,
    )
    observed = subject._diagnose_model_migration(
        evidence(),
        policy=policy(),
        simulations=100,
        seed=11,
        _qualification=qualified,
        _qualification_checked_at=NOW,
        _observer=lambda _record: None,
    )

    assert observed == plain
    assert len(created) == 2
    assert created[0].getstate() == created[1].getstate()


def test_observer_failure_aborts_private_experiment():
    def fail(_record):
        raise RuntimeError("observer unavailable")

    with pytest.raises(RuntimeError, match="observer unavailable"):
        subject._diagnose_model_migration(
            evidence(),
            policy=policy(),
            simulations=100,
            seed=11,
            _qualification=qualification(),
            _qualification_checked_at=NOW,
            _observer=fail,
        )


def test_drift_effect_just_above_point_two_is_strictly_eligible():
    shift = 0.20 + 5e-13
    assessed = subject._assess_experimental_demand_readiness(
        [
            *[bundle(index) for index in range(36)],
            *[bundle(index, shift=shift) for index in range(36, 40)],
        ],
        qualification=qualification(),
    )
    first_batch = assessed.monitoring_batches[0]

    assert first_batch.effect > 0.20
    assert first_batch.p_value <= first_batch.alpha
    assert first_batch.critical
    assert assessed.state == "critical"
