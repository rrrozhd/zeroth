"""Interval estimators for the economic plane.

Every estimator here is a plain function over samples, with no persistence and no
settings. The module exists because the plane's earlier estimators decided on point
estimates, quantized the requested confidence to two z values, shrank means toward a
zero prior, and bootstrapped from two observations. Each function documents the
population statement it makes and the sample sizes at which that statement has been
checked by simulation (``tests/econ_plane/test_estimator_validity.py``).

The Student-t quantile is computed here rather than imported because the platform does
not depend on SciPy; it inverts the regularized incomplete beta function, which is exact
to the precision of the bisection (``1e-12`` in ``t``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral
from statistics import NormalDist

import numpy as np

#: Below this many observations the bootstrap is replaced by a Student-t interval and
#: the confidence gate stays closed. Percentile bootstrap coverage of a 95% interval is
#: 0.50 at n=2 and 0.81 at n=10 on skewed data; the bootstrap-t reaches 0.92 at n=30.
MIN_BOOTSTRAP_SAMPLE = 30

#: Fixed seed so that a persisted estimate can be reproduced from its inputs.
BOOTSTRAP_SEED = 7
BOOTSTRAP_ITERATIONS = 800

#: ``mean_shift_score`` thresholds, in standard-error units of the difference between
#: the recent window and the earlier observations. A stationary series exceeds 2.0 with
#: probability about 0.05 and 3.0 with probability about 0.003.
DRIFT_WARNING_SCORE = 2.0
DRIFT_CRITICAL_SCORE = 3.0

#: Coefficient of variation above which a value series is flagged as high-variance.
#: A binary $120/$0 proxy has CV ``sqrt((1 - p) / p)``: 1.0 at p=0.5, 1.5 at p=0.3,
#: 2.0 at p=0.2; the flag names rare-positive or heavy-tailed proxies, not sample noise.
HIGH_VARIANCE_CV = 2.0

#: Cap on scores that would otherwise be infinite (zero-variance windows), so that the
#: persisted float and its JSON serialisation stay finite.
_SCORE_CAP = 100.0

#: Cap on ``required_sample_size`` so a zero distance from a threshold reports a large
#: finite number instead of overflowing an integer column.
MAX_REQUIRED_SAMPLE = 1_000_000


def validate_confidence(confidence: float) -> float:
    """Return ``confidence`` when it is a probability strictly inside ``(0, 1)``."""
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("confidence_level must be a number strictly between 0 and 1")
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("confidence_level must be strictly between 0 and 1")
    return float(confidence)


def z_quantile(confidence: float) -> float:
    """Two-sided standard-normal quantile: ``P(|Z| <= z) == confidence``."""
    return -NormalDist().inv_cdf((1.0 - validate_confidence(confidence)) / 2.0)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz evaluation of the continued fraction for the incomplete beta function."""
    max_iterations, epsilon, tiny = 400, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)`` for ``a, b > 0`` and ``0 <= x <= 1``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(t: float, degrees_of_freedom: float) -> float:
    """``P(T <= t)`` for a Student-t variable with ``degrees_of_freedom``."""
    if degrees_of_freedom <= 0:
        raise ValueError("degrees_of_freedom must be positive")
    x = degrees_of_freedom / (degrees_of_freedom + t * t)
    tail = 0.5 * regularized_incomplete_beta(degrees_of_freedom / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def t_quantile(confidence: float, degrees_of_freedom: float) -> float:
    """Two-sided Student-t quantile: ``P(|T| <= t) == confidence``.

    Bisection on ``student_t_cdf``; converges to ``1e-12`` in ``t``. Above one million
    degrees of freedom the normal quantile is returned, where the two agree to better
    than ``1e-6``.
    """
    probability = (1.0 + validate_confidence(confidence)) / 2.0
    if degrees_of_freedom <= 0:
        raise ValueError("degrees_of_freedom must be positive")
    if degrees_of_freedom > 1_000_000:
        return NormalDist().inv_cdf(probability)
    low, high = 0.0, 1.0
    while student_t_cdf(high, degrees_of_freedom) < probability:
        high *= 2.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if student_t_cdf(mid, degrees_of_freedom) < probability:
            low = mid
        else:
            high = mid
        if high - low < 1e-12:
            break
    return 0.5 * (low + high)


@dataclass(frozen=True)
class MeanInterval:
    """A two-sided interval for a population mean.

    ``defined`` is false when the sample cannot support a width at all (fewer than two
    observations); the bounds then equal the point estimate and must not be read as a
    zero-width certainty.
    """

    mean: float
    low: float
    high: float
    sample_size: int
    method: str
    defined: bool

    @property
    def half_width(self) -> float:
        """Half the interval width."""
        return (self.high - self.low) / 2.0


def student_t_mean_interval(values: Sequence[float], confidence: float = 0.95) -> MeanInterval:
    """Student-t interval for the mean using the sample standard deviation.

    No prior shrinkage: the point estimate is the sample mean. Coverage of a 95%
    interval is 0.94 at n=5 and 0.95 at n>=20 on normal data; on strongly skewed data it
    under-covers below n=30 (0.85 at n=10 for lognormal(2, 1)), which is why the plane's
    confidence gate refuses samples that small regardless of the reported width.
    """
    confidence = validate_confidence(confidence)
    sample = np.asarray(list(values), dtype=float)
    n = int(sample.size)
    if n == 0:
        return MeanInterval(0.0, 0.0, 0.0, 0, "undefined", False)
    mean = float(sample.mean())
    if n == 1:
        return MeanInterval(mean, mean, mean, 1, "undefined_single_sample", False)
    half = t_quantile(confidence, n - 1) * float(sample.std(ddof=1)) / math.sqrt(n)
    return MeanInterval(mean, mean - half, mean + half, n, "student_t", True)


def bootstrap_t_mean_interval(
    values: Sequence[float],
    confidence: float = 0.95,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> MeanInterval:
    """Studentized (bootstrap-t) interval for the mean.

    Each resample contributes ``t* = (mean* - mean) / (sd* / sqrt(n))``; the interval is
    ``mean - q_hi * se`` to ``mean - q_lo * se`` with ``se = sd / sqrt(n)``. Unlike the
    percentile bootstrap it corrects for skew: coverage of a 95% interval on
    lognormal(2, 1) data is 0.92 at n=30 and 0.94 at n=100, against 0.885 and 0.916 for
    the percentile method. Below two observations it degrades to
    ``student_t_mean_interval``; a zero sample standard deviation yields a zero-width
    interval because every resample is identical.
    """
    confidence = validate_confidence(confidence)
    sample = np.asarray(list(values), dtype=float)
    n = int(sample.size)
    if n < 2:
        return student_t_mean_interval(sample, confidence)
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0.0:
        return MeanInterval(mean, mean, mean, n, "bootstrap_t", True)
    rng = np.random.default_rng(seed)
    index = rng.integers(0, n, size=(iterations, n))
    resamples = sample[index]
    resample_means = resamples.mean(axis=1)
    resample_sd = resamples.std(axis=1, ddof=1)
    resample_sd = np.where(resample_sd == 0.0, 1e-12, resample_sd)
    pivots = (resample_means - mean) / (resample_sd / math.sqrt(n))
    alpha = (1.0 - confidence) / 2.0
    q_low = float(np.quantile(pivots, alpha))
    q_high = float(np.quantile(pivots, 1.0 - alpha))
    se = sd / math.sqrt(n)
    return MeanInterval(mean, mean - q_high * se, mean - q_low * se, n, "bootstrap_t", True)


def wilson_interval(
    successes: int, n: int, confidence: float = 0.95
) -> tuple[float, float, float]:
    """Wilson score interval for a binomial proportion.

    Returns ``(p_hat, low, high)``. An empty sample returns the whole unit interval.
    Coverage tracks the requested level to within 0.03 at n=100 for 0.8 through 0.99.
    """
    confidence = validate_confidence(confidence)
    if (
        not isinstance(successes, Integral) or isinstance(successes, bool)
        or not isinstance(n, Integral) or isinstance(n, bool)
        or not 0 <= successes <= n
    ):
        raise ValueError("counts must be integers satisfying 0 <= successes <= n")
    if n == 0:
        return 0.0, 0.0, 1.0
    z = z_quantile(confidence)
    p_hat = successes / n
    denominator = 1.0 + z * z / n
    centre = (p_hat + z * z / (2.0 * n)) / denominator
    radius = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - radius)
    high = 1.0 if successes == n else min(1.0, centre + radius)
    return p_hat, low, high


def newcombe_difference_interval(
    baseline_successes: int,
    baseline_n: int,
    candidate_successes: int,
    candidate_n: int,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """Newcombe hybrid-score interval for ``p_candidate - p_baseline``.

    Combines the two Wilson intervals (Newcombe 1998, method 10). Coverage of a 95%
    interval is at or above nominal for n>=10 and conservative near 0 and 1.
    """
    p_base, low_base, high_base = wilson_interval(baseline_successes, baseline_n, confidence)
    p_cand, low_cand, high_cand = wilson_interval(candidate_successes, candidate_n, confidence)
    difference = p_cand - p_base
    lower = difference - math.sqrt((p_cand - low_cand) ** 2 + (high_base - p_base) ** 2)
    upper = difference + math.sqrt((high_cand - p_cand) ** 2 + (p_base - low_base) ** 2)
    return difference, max(-1.0, lower), min(1.0, upper)


def log_ratio_of_sums_variance(numerators: Sequence[float], denominators: Sequence[float]) -> float:
    """Delta-method variance of ``log(sum(numerators) / sum(denominators))``.

    The pairs are treated as an i.i.d. sample, so the covariance between a run's cost
    and its acceptance is kept. Requires at least two pairs and a positive mean in both
    sequences.
    """
    num = np.asarray(list(numerators), dtype=float)
    den = np.asarray(list(denominators), dtype=float)
    if num.size != den.size or num.size < 2:
        raise ValueError("at least two paired observations are required")
    n = int(num.size)
    num_mean, den_mean = float(num.mean()), float(den.mean())
    if num_mean <= 0.0 or den_mean <= 0.0:
        raise ValueError("both sequences must have a positive mean")
    covariance = np.cov(num, den, ddof=1)
    variance = (
        float(covariance[0, 0]) / num_mean**2
        + float(covariance[1, 1]) / den_mean**2
        - 2.0 * float(covariance[0, 1]) / (num_mean * den_mean)
    )
    return max(variance, 0.0) / n


def ratio_change_interval(
    baseline_numerators: Sequence[float],
    baseline_denominators: Sequence[float],
    candidate_numerators: Sequence[float],
    candidate_denominators: Sequence[float],
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """Interval for the relative change of a ratio of sums between two samples.

    Returns ``(change, low, high)`` where ``change = candidate_ratio / baseline_ratio - 1``.
    The interval is symmetric on the log scale, with a Student-t quantile on
    ``n_baseline + n_candidate - 2`` degrees of freedom. Coverage of a 95% interval for a
    cost-per-accepted-outcome ratio (acceptance 0.9, lognormal cost, CV 0.53) is 0.95 at
    n=10 per version and 0.95 at n>=30.
    """
    confidence = validate_confidence(confidence)
    base_ratio = float(np.sum(baseline_numerators)) / float(np.sum(baseline_denominators))
    cand_ratio = float(np.sum(candidate_numerators)) / float(np.sum(candidate_denominators))
    log_change = math.log(cand_ratio) - math.log(base_ratio)
    variance = log_ratio_of_sums_variance(
        baseline_numerators, baseline_denominators
    ) + log_ratio_of_sums_variance(candidate_numerators, candidate_denominators)
    degrees = len(baseline_numerators) + len(candidate_numerators) - 2
    half = t_quantile(confidence, degrees) * math.sqrt(variance)
    return (
        math.exp(log_change) - 1.0,
        math.exp(log_change - half) - 1.0,
        math.exp(log_change + half) - 1.0,
    )


def relative_interval_width(mean_value: float, low: float, high: float) -> float:
    """Interval width divided by the magnitude of the point estimate."""
    denominator = max(abs(mean_value), 1e-6)
    return max(0.0, (high - low) / denominator)


def required_sample_size(
    variance_per_observation: float, distance: float, confidence: float = 0.95
) -> int:
    """Observations per group for a two-sided interval to exclude a threshold.

    ``variance_per_observation`` is the variance of the compared statistic contributed by
    one observation in each group (so that the statistic's variance at size ``n`` is
    ``variance_per_observation / n``), and ``distance`` is how far the current point
    estimate sits from the threshold. The answer is ``z^2 * variance / distance^2``,
    capped at ``MAX_REQUIRED_SAMPLE``.
    """
    z = z_quantile(confidence)
    if distance <= 0.0 or variance_per_observation <= 0.0:
        return MAX_REQUIRED_SAMPLE if variance_per_observation > 0.0 else 0
    required = math.ceil(z * z * variance_per_observation / (distance * distance))
    return int(min(required, MAX_REQUIRED_SAMPLE))


def coefficient_of_variation(values: Sequence[float]) -> float:
    """Sample standard deviation over the absolute mean; 0.0 below two observations."""
    sample = np.asarray(list(values), dtype=float)
    if sample.size < 2:
        return 0.0
    sd = float(sample.std(ddof=1))
    mean = abs(float(sample.mean()))
    if sd == 0.0:
        return 0.0
    if mean == 0.0:
        return _SCORE_CAP
    return min(sd / mean, _SCORE_CAP)


def mean_shift_score(
    ordered_values: Sequence[float],
    *,
    minimum_size: int = 10,
    window_fraction: float = 0.2,
    minimum_window: int = 5,
) -> float:
    """Shift between the most recent window and the earlier observations, in SE units.

    The window is the last ``max(minimum_window, round(window_fraction * n))``
    observations; the score is the two-sample t statistic
    ``|mean(recent) - mean(earlier)| / (s_pooled * sqrt(1/k + 1/(n - k)))``. The pooled
    variance is deliberate: under the no-drift hypothesis both segments share one
    variance, and a per-segment (Welch) estimate lets a short window with no failures
    report zero variance and spike the score on discreteness alone (critical rate 0.054
    at p=0.9 against 0.006 pooled, on a stationary $120/$0 proxy with n=100). Fewer than
    ``minimum_size`` observations score 0.0: there is no window to compare. Two identical
    constant segments score 0.0 and two different constant segments score ``_SCORE_CAP``.
    """
    sample = np.asarray(list(ordered_values), dtype=float)
    n = int(sample.size)
    if n < minimum_size:
        return 0.0
    window = max(minimum_window, int(round(window_fraction * n)))
    window = min(window, n - 2)
    recent, earlier = sample[-window:], sample[:-window]
    if recent.size < 2 or earlier.size < 2:
        return 0.0
    difference = float(recent.mean() - earlier.mean())
    pooled_variance = (
        (recent.size - 1) * float(recent.var(ddof=1))
        + (earlier.size - 1) * float(earlier.var(ddof=1))
    ) / (n - 2)
    se = math.sqrt(pooled_variance * (1.0 / recent.size + 1.0 / earlier.size))
    if se == 0.0:
        return 0.0 if difference == 0.0 else _SCORE_CAP
    return min(abs(difference) / se, _SCORE_CAP)


def drift_state(score: float) -> str:
    """Map a ``mean_shift_score`` to ``stable``, ``warning`` or ``critical``."""
    if score >= DRIFT_CRITICAL_SCORE:
        return "critical"
    if score >= DRIFT_WARNING_SCORE:
        return "warning"
    return "stable"


def binary_dollar_interval(
    positives: int,
    n: int,
    value_positive: float,
    value_negative: float,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """Dollar interval for ``n`` binary outcomes worth ``value_positive`` or ``value_negative``.

    The Wilson band on the positive rate is mapped through the two known per-class
    values, so the interval never collapses when a class has not been observed yet: with
    zero positives the upper bound still carries the positive class value. The bounds
    are sorted because a mixed-sign proxy inverts the mapping.
    """
    _p_hat, low_p, high_p = wilson_interval(positives, n, confidence)
    estimate = positives * value_positive + (n - positives) * value_negative

    def dollars(p: float) -> float:
        return n * (p * value_positive + (1.0 - p) * value_negative)

    low, high = sorted((dollars(low_p), dollars(high_p)))
    return estimate, low, high


__all__ = [
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "DRIFT_CRITICAL_SCORE",
    "DRIFT_WARNING_SCORE",
    "HIGH_VARIANCE_CV",
    "MAX_REQUIRED_SAMPLE",
    "MIN_BOOTSTRAP_SAMPLE",
    "MeanInterval",
    "binary_dollar_interval",
    "bootstrap_t_mean_interval",
    "coefficient_of_variation",
    "drift_state",
    "log_ratio_of_sums_variance",
    "mean_shift_score",
    "newcombe_difference_interval",
    "ratio_change_interval",
    "regularized_incomplete_beta",
    "relative_interval_width",
    "required_sample_size",
    "student_t_cdf",
    "student_t_mean_interval",
    "t_quantile",
    "validate_confidence",
    "wilson_interval",
    "z_quantile",
]
