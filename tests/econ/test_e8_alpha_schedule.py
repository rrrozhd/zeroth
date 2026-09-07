"""The E8 drift monitor keeps a non-empty rejection region for every early batch.

The exact permutation test of one monitoring batch against 24 calibration periods
cannot produce a p-value below 1/C(24 + batch, batch). With three-period batches and
the harmonic schedule 0.0025/(k(k+1)) that floor (3.42e-4) exceeded the alpha of every
batch from the third on, so the monitor could never fire after two batches. Four-period
batches with geometric spending 0.0005 * 0.8**(k-1) keep batches 1..11 attainable while
the per-metric total stays exactly at 0.0025.
"""

from __future__ import annotations

from math import comb

import pytest

import zeroth.econ.probabilistic as subject
from tests.econ.test_experimental_e8_private import bundle, qualification

FLOOR = 1 / comb(24 + 4, 4)


def test_every_batch_through_the_eighth_is_attainable():
    assert subject._EXPERIMENTAL_MONITORING_BATCH_SIZE == 4
    assert subject._experimental_minimum_attainable_p_value() == FLOOR
    for batch_index in range(1, 9):
        assert subject._experimental_drift_alpha(batch_index) >= FLOOR
        assert subject._experimental_drift_batch_attainable(batch_index)
    assert subject._experimental_drift_batch_attainable(11)
    assert not subject._experimental_drift_batch_attainable(12)


def test_per_metric_alpha_total_stays_within_budget():
    spent = sum(subject._experimental_drift_alpha(batch) for batch in range(1, 5_000))

    assert spent <= 0.0025 + 1e-12
    assert spent == pytest.approx(0.0025, rel=1e-9)
    assert subject._experimental_drift_alpha(1) == pytest.approx(0.0005)
    assert subject._experimental_drift_alpha(2) == pytest.approx(0.0004)


def test_the_replaced_harmonic_schedule_was_unattainable_from_batch_three():
    three_period_floor = 1 / comb(27, 3)
    harmonic = [(0.01 / 4) / (batch * (batch + 1)) for batch in range(1, 9)]

    assert harmonic[0] >= three_period_floor and harmonic[1] >= three_period_floor
    assert all(alpha < three_period_floor for alpha in harmonic[2:])


def test_shift_confined_to_the_third_batch_is_detected():
    bundles = [
        *[bundle(index) for index in range(36)],
        *[bundle(index) for index in range(36, 44)],
        *[bundle(index, shift=0.25) for index in range(44, 48)],
    ]
    assessed = subject._assess_experimental_demand_readiness(
        bundles, qualification=qualification()
    )
    cost_batches = [
        batch for batch in assessed.monitoring_batches if batch.metric == "monthly_cost_usd"
    ]

    assert [batch.batch_index for batch in cost_batches] == [1, 2, 3]
    assert [batch.critical for batch in cost_batches] == [False, False, True]
    assert cost_batches[2].p_value == pytest.approx(FLOOR)
    assert cost_batches[2].p_value <= cost_batches[2].alpha
    assert assessed.state == "critical"
    assert assessed.reason == "calibration_drift_critical"


def test_a_three_period_tail_is_an_incomplete_batch():
    assessed = subject._assess_experimental_demand_readiness(
        [bundle(index) for index in range(39)], qualification=qualification()
    )

    assert assessed.state == "unknown"
    assert assessed.reason == "monitoring_batch_incomplete"


def test_permutation_test_rejects_wrong_batch_sizes():
    with pytest.raises(ValueError, match="4 monitoring"):
        subject._exact_experimental_permutation_pvalue([0.0] * 24, [0.0] * 3)
