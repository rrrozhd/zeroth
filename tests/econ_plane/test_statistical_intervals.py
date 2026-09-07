"""Analytical checks; these do not certify a population authorization rule."""

from __future__ import annotations

import math

import pytest

from zeroth.econ.plane.statistics.service import hierarchical_interval, wilson_interval


@pytest.mark.parametrize(
    "confidence, z",
    [(0.50, 0.6744897501960817), (0.90, 1.6448536269514722),
     (0.95, 1.959963984540054), (0.99, 2.5758293035489004)],
)
@pytest.mark.parametrize("n", [1, 2, 5, 25, 1000])
def test_wilson_endpoints_invert_the_score_test(confidence, z, n) -> None:
    # NIST e-Handbook 7.2.4.1: invert the score test, rather than reproduce
    # the production center/radius formula. Fixed normal quantiles are references.
    for successes in {0, n // 2, n}:
        center, low, high = wilson_interval(successes, n, confidence)
        assert 0 <= low < high <= 1
        assert low <= successes / n <= high
        assert center == pytest.approx((low + high) / 2)
        for endpoint in (low, high):
            assert n * (endpoint - successes / n) ** 2 == pytest.approx(
                z**2 * endpoint * (1 - endpoint), rel=1e-9, abs=1e-12
            )


@pytest.mark.parametrize("successes, n", [(0, 1), (1, 1), (3, 5), (12, 25), (999, 1000)])
def test_higher_wilson_confidence_never_narrows_the_interval(successes, n) -> None:
    intervals = [wilson_interval(successes, n, c) for c in (0.8, 0.9, 0.95, 0.99)]
    for (_, low, high), (_, wider_low, wider_high) in zip(intervals, intervals[1:], strict=False):
        assert wider_low <= low + 1e-15
        assert wider_high >= high - 1e-15
        assert wider_high - wider_low > high - low


@pytest.mark.parametrize("confidence", [0, 1, -0.1, 1.1, math.nan, math.inf])
def test_wilson_rejects_invalid_confidence_even_without_observations(confidence) -> None:
    for n in (0, 5):
        with pytest.raises(ValueError, match="confidence"):
            wilson_interval(0, n, confidence)


@pytest.mark.parametrize("successes, n", [(-1, 5), (6, 5), (0, -1), (1, 0), (0.5, 5), (1, 5.5)])
def test_wilson_rejects_impossible_or_fractional_counts(successes, n) -> None:
    with pytest.raises(ValueError, match="count"):
        wilson_interval(successes, n)


def test_no_binomial_observations_remain_uninformative() -> None:
    assert wilson_interval(0, 0) == (0, 0, 1)


def test_legacy_mean_heuristic_also_respects_requested_normal_quantiles() -> None:
    # This checks quantile plumbing, not the heuristic's Bayesian validity.
    _, low95, high95 = hierarchical_interval([1, 2, 3, 4], confidence=0.95)
    _, low99, high99 = hierarchical_interval([1, 2, 3, 4], confidence=0.99)
    assert (high99 - low99) / (high95 - low95) == pytest.approx(
        2.5758293035489004 / 1.959963984540054
    )
