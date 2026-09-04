"""Decimal-probability rank and finite empirical tail arithmetic contracts."""

from collections import Counter
from fractions import Fraction
from math import inf, isfinite, nextafter
import sys

import pytest

from zeroth.econ.probabilistic import (
    _empirical_quantile,
    _histogram_quantile,
    empirical_var_cvar,
)


def _rank_paths(values, probability):
    return (
        _empirical_quantile(values, probability),
        empirical_var_cvar(values, confidence=probability)[0],
        _histogram_quantile(dict(Counter(values)), len(values), probability),
    )


@pytest.mark.parametrize("percent", [7, 14, 28, 56, 95])
@pytest.mark.parametrize("factor", [1, 2, 3, 5, 10])
def test_decimal_percent_rank_and_replication(percent, factor):
    values = list(range(100)) * factor
    assert _rank_paths(values, percent / 100) == (percent - 1,) * 3


@pytest.mark.parametrize("percent", [7, 14, 28, 56, 95])
@pytest.mark.parametrize("factor", [1, 5, 10])
def test_adjacent_probabilities_are_not_merged(percent, factor):
    probability = percent / 100
    values = list(range(100)) * factor
    assert _rank_paths(values, nextafter(probability, -inf)) == (percent - 1,) * 3
    assert _rank_paths(values, probability) == (percent - 1,) * 3
    assert _rank_paths(values, nextafter(probability, inf)) == (percent,) * 3


def test_quantile_closed_endpoints():
    assert _empirical_quantile([2, 8], 0) == _histogram_quantile({2: 1, 8: 1}, 2, 0) == 2
    assert _empirical_quantile([2, 8], 1) == _histogram_quantile({2: 1, 8: 1}, 2, 1) == 8


@pytest.mark.parametrize("confidence", [0.01, 0.5, 0.95, nextafter(0.0, 1.0), nextafter(1.0, 0.0)])
@pytest.mark.parametrize("size", [2, 4, 100])
@pytest.mark.parametrize("value", [1e308, -1e308, sys.float_info.max, 1e-308, nextafter(0.0, 1.0)])
def test_identical_finite_losses_have_identical_finite_tail(value, size, confidence):
    result = empirical_var_cvar([value] * size, confidence=confidence)
    assert all(isfinite(v) for v in result)
    assert result == (value, value)


def _integrated_tail_reference(values, confidence):
    """Integrate the empirical quantile over [alpha,1] using exact CDF cells."""
    alpha = Fraction(str(confidence))
    n = len(values)
    area = Fraction(0)
    for i, value in enumerate(sorted(values)):
        overlap = max(Fraction(0), Fraction(i + 1, n) - max(alpha, Fraction(i, n)))
        area += overlap * Fraction(float(value))
    return float(area / (1 - alpha))


@pytest.mark.parametrize(
    "values",
    [
        [-1e308, 1e-100, 1e308],
        [-sys.float_info.max, sys.float_info.max],
        [-1e308, -1e308, 1e308, 1e308],
        [-1e-308, 0.0, 1e-308],
        [0.0, nextafter(0.0, 1.0)],
        [-10.0, 0.0, 10.0],
        [0.0, 1.0, 2.0, 3.0],
    ],
)
@pytest.mark.parametrize(
    "confidence", [nextafter(0.0, 1.0), 0.01, 0.07, 0.5, 0.625, 0.95, nextafter(1.0, 0.0)]
)
def test_mixed_sign_and_fractional_tail_matches_exact_cdf_integral(values, confidence):
    var, cvar = empirical_var_cvar(values, confidence=confidence)
    assert isfinite(var) and isfinite(cvar)
    assert cvar == _integrated_tail_reference(values, confidence)
    assert min(values) <= var <= cvar <= max(values)
    assert empirical_var_cvar(values * 5, confidence=confidence) == (var, cvar)
    assert empirical_var_cvar(values[::-1], confidence=confidence) == (var, cvar)


@pytest.mark.parametrize("confidence", [0.07, 0.5, 0.625, 0.95])
def test_tail_translation_and_positive_scaling(confidence):
    values = [-8.0, -4.0, 0.0, 4.0, 8.0]
    base = empirical_var_cvar(values, confidence=confidence)
    transformed = empirical_var_cvar([v / 4 + 16 for v in values], confidence=confidence)
    assert transformed == pytest.approx([v / 4 + 16 for v in base], rel=2e-15, abs=0)


def test_fractional_boundary_hand_answers():
    assert empirical_var_cvar([0.0] * 99 + [1000.0], confidence=0.95) == (0.0, 200.0)
    assert empirical_var_cvar([0.0, 1.0, 2.0, 3.0], confidence=0.625) == (2.0, 8 / 3)
    assert empirical_var_cvar([-1e308] * 99 + [1e308], confidence=0.98) == (-1e308, 0.0)


@pytest.mark.parametrize("confidence", [0.0, 1.0, -0.1, 1.1, float("nan"), inf, -inf])
def test_cvar_confidence_open_endpoints_and_invalid_values(confidence):
    with pytest.raises(ValueError):
        empirical_var_cvar([1.0], confidence=confidence)


@pytest.mark.parametrize("losses", [[], [inf], [-inf], [float("nan")]])
def test_nonfinite_or_empty_losses_rejected(losses):
    with pytest.raises(ValueError):
        empirical_var_cvar(losses, confidence=0.95)
