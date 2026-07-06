from __future__ import annotations

from math import sqrt
from statistics import mean, pstdev

import numpy as np


def hierarchical_interval(values: list[float], prior_mean: float = 0.0, prior_weight: float = 5.0, confidence: float = 0.95) -> tuple[float, float, float]:
    if not values:
        return prior_mean, prior_mean - 1.0, prior_mean + 1.0

    n = len(values)
    sample_mean = mean(values)
    post_mean = ((prior_mean * prior_weight) + (sample_mean * n)) / (prior_weight + n)
    sigma = pstdev(values) if n > 1 else max(abs(sample_mean) * 0.25, 1.0)
    se = sigma / sqrt(max(n, 1))
    z = 1.96 if confidence >= 0.95 else 1.64
    return post_mean, post_mean - z * se, post_mean + z * se


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float, float]:
    if n <= 0:
        return 0.0, 0.0, 1.0
    z = 1.96 if confidence >= 0.95 else 1.64
    phat = successes / n
    denom = 1 + (z * z / n)
    center = (phat + (z * z) / (2 * n)) / denom
    radius = (z * sqrt((phat * (1 - phat) / n) + (z * z / (4 * n * n)))) / denom
    return center, max(0.0, center - radius), min(1.0, center + radius)


def bootstrap_interval(values: list[float], confidence: float = 0.95, iters: int = 800) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    arr = np.array(values, dtype=float)
    if len(arr) == 1:
        v = float(arr[0])
        return v, v, v
    rng = np.random.default_rng(7)
    sample_means = []
    for _ in range(iters):
        idx = rng.integers(0, len(arr), size=len(arr))
        sample_means.append(float(arr[idx].mean()))
    lower_q = (1.0 - confidence) / 2.0
    upper_q = 1.0 - lower_q
    return float(arr.mean()), float(np.quantile(sample_means, lower_q)), float(np.quantile(sample_means, upper_q))


def relative_interval_width(mean_value: float, low: float, high: float) -> float:
    denom = max(abs(mean_value), 1e-6)
    return max(0.0, (high - low) / denom)


def confidence_gate(confidence_level: float, rel_width: float, min_conf: float = 0.95, max_rel: float = 0.30) -> bool:
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
    sample_size_ok = sample_size >= 30
    variance_ok = variance <= 1.0
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
