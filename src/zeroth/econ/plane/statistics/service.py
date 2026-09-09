"""Statistics helpers used by the costing and counterfactual services.

The estimators live in :mod:`zeroth.econ.plane.statistics.intervals`; this module keeps
the names the rest of the plane imports and the confidence-gate policy.
"""

from __future__ import annotations

from collections.abc import Sequence

from zeroth.econ.plane.statistics.intervals import (
    BOOTSTRAP_ITERATIONS,
    HIGH_VARIANCE_CV,
    MIN_BOOTSTRAP_SAMPLE,
    bootstrap_t_mean_interval,
    relative_interval_width,
    student_t_mean_interval,
    wilson_interval,
)

__all__ = [
    "bootstrap_interval",
    "build_confidence_breakdown",
    "confidence_gate",
    "hierarchical_interval",
    "mean_interval",
    "relative_interval_width",
    "wilson_interval",
]


def mean_interval(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float, float]:
    """Student-t interval for the mean as ``(mean, low, high)``.

    A single observation returns its value three times; callers that need to know the
    width is undefined should use ``student_t_mean_interval`` and read ``defined``.
    """
    estimate = student_t_mean_interval(values, confidence)
    return estimate.mean, estimate.low, estimate.high


def hierarchical_interval(
    values: Sequence[float],
    prior_mean: float = 0.0,
    prior_weight: float = 5.0,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """Compatibility name for :func:`mean_interval`.

    The estimator this name used to denote shrank the sample mean toward ``prior_mean``
    by ``n / (n + prior_weight)`` and covered the true mean 1% to 65% of the time at a
    nominal 95%. The prior arguments are accepted and ignored so existing imports keep
    working; the returned interval is the Student-t interval on the sample.
    """
    del prior_mean, prior_weight
    return mean_interval(values, confidence)


def bootstrap_interval(
    values: Sequence[float], confidence: float = 0.95, iters: int = BOOTSTRAP_ITERATIONS
) -> tuple[float, float, float]:
    """Bootstrap-t interval for the mean as ``(mean, low, high)``.

    Below ``MIN_BOOTSTRAP_SAMPLE`` observations the Student-t interval is returned
    instead; the resampling distribution of a handful of points does not represent the
    population it was drawn from.
    """
    if len(values) < MIN_BOOTSTRAP_SAMPLE:
        return mean_interval(values, confidence)
    estimate = bootstrap_t_mean_interval(values, confidence, iterations=iters)
    return estimate.mean, estimate.low, estimate.high


def confidence_gate(
    confidence_level: float,
    rel_width: float,
    min_conf: float = 0.95,
    max_rel: float = 0.30,
    *,
    sample_size: int | None = None,
    min_sample_size: int = MIN_BOOTSTRAP_SAMPLE,
) -> bool:
    """Whether an interval is tight enough, at a high enough level, on enough data.

    ``confidence_level`` is the nominal level the interval was computed at. Requiring
    it to reach ``min_conf`` is a precondition for the width test, not evidence: a
    narrower interval at a lower level says nothing about precision at the level the
    gate is configured for. The evidence is ``rel_width`` and ``sample_size``; below
    ``min_sample_size`` no reported width is trusted because the small-sample
    estimators under-cover (see ``MIN_BOOTSTRAP_SAMPLE``).
    """
    if sample_size is not None and sample_size < min_sample_size:
        return False
    return confidence_level >= min_conf and rel_width <= max_rel


def build_confidence_breakdown(
    sample_size: int,
    baseline_ok: bool,
    variance: float,
    calibration_ok: bool,
    provenance_ok: bool,
    drift_state: str,
    cost_data_quality: str,
) -> dict:
    """Reason codes explaining why an estimate is or is not trustworthy.

    ``variance`` is the coefficient of variation of the per-outcome values; the
    ``HIGH_VARIANCE`` code marks a rare-positive or heavy-tailed proxy
    (``HIGH_VARIANCE_CV``), not sample noise, which ``LOW_N`` and the relative width
    already cover.
    """
    sample_size_ok = sample_size >= MIN_BOOTSTRAP_SAMPLE
    variance_ok = variance <= HIGH_VARIANCE_CV
    drift_ok = drift_state != "critical"

    reason_codes: list[str] = []
    if not sample_size_ok:
        reason_codes.append("LOW_N")
    if not baseline_ok:
        reason_codes.append("BASELINE_WEAK")
    if not variance_ok:
        reason_codes.append("HIGH_VARIANCE")
    if cost_data_quality != "measured":
        reason_codes.append("MISSING_MEASURED_COST")
    if drift_state == "warning":
        reason_codes.append("DRIFT_WARNING")
    if drift_state == "critical":
        reason_codes.append("DRIFT_CRITICAL")
    if not calibration_ok:
        reason_codes.append("CALIBRATION_BIAS")
    if not provenance_ok:
        reason_codes.append("PROVENANCE_INFERRED_HEAVY")

    return {
        "sample_size_ok": sample_size_ok,
        "baseline_ok": baseline_ok,
        "variance_ok": variance_ok,
        "calibration_ok": calibration_ok,
        "provenance_ok": provenance_ok,
        "drift_ok": drift_ok,
        "reason_codes": reason_codes,
    }
