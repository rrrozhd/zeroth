"""Evaluator-owned laws and summaries. No production imports or candidate fitting."""

import math
import random
from fractions import Fraction
from functools import lru_cache
from statistics import fmean

from release.economic_evaluation.reference import FiniteLaw

METRICS = ("monthly_cost_usd", "success_rate", "p95_latency_ms", "critical_error_rate")


def outcome(world, hard=False):
    if world in (0, 1):
        return ((1.0, 100, True, False), (0.6, 100, True, False))
    if world == 2:
        return (
            ((3.0, 400, True, False), (2.0, 800, False, True))
            if hard
            else ((1.0, 100, True, False), (0.6, 80, True, False))
        )
    raise ValueError("unknown predictive world")


def generate_pairs(world, size, rng):
    return [outcome(world, rng.random() >= 0.8 if world == 2 else False) for _ in range(size)]


def demand(world, rng):
    return (1000 if rng.random() < 0.5 else 2000) if world == 1 else 1000


def quantile(values, probability):
    if not values or not 0 <= probability <= 1:
        raise ValueError("nonempty finite quantile input required")
    return sorted(values)[max(0, math.ceil(probability * len(values)) - 1)]


def monthly(pairs):
    if not pairs:
        raise ValueError("month requires requests")
    result = {}
    for arm, name in enumerate(("incumbent", "candidate")):
        rows = [pair[arm] for pair in pairs]
        result[name] = dict(
            zip(
                METRICS,
                (
                    math.fsum(row[0] for row in rows),
                    fmean(row[2] for row in rows),
                    quantile([row[1] for row in rows], 0.95),
                    fmean(row[3] for row in rows),
                ),
                strict=True,
            )
        )
    result["savings"] = result["incumbent"][METRICS[0]] - result["candidate"][METRICS[0]]
    result["quality_drop"] = result["incumbent"][METRICS[1]] - result["candidate"][METRICS[1]]
    result["loss"] = -result["savings"] + 300 * sum(pair[1][3] - pair[0][3] for pair in pairs)
    return result


@lru_cache(maxsize=3)
def true_laws(world):
    if world in (0, 1):
        demand_atoms = (
            [(1000, Fraction(1))]
            if world == 0
            else [(1000, Fraction(1, 2)), (2000, Fraction(1, 2))]
        )
        return {
            "monthly_cost_usd": FiniteLaw([(Fraction(3, 5) * d, p) for d, p in demand_atoms]),
            "success_rate": FiniteLaw([(1, 1)]),
            "p95_latency_ms": FiniteLaw([(100, 1)]),
            "critical_error_rate": FiniteLaw([(0, 1)]),
            "quality_drop": FiniteLaw([(0, 1)]),
            "loss": FiniteLaw([(-Fraction(2, 5) * d, p) for d, p in demand_atoms]),
            "incumbent_cost": FiniteLaw(demand_atoms),
        }
    if world != 2:
        raise ValueError("unknown predictive world")
    states = [(h, Fraction(math.comb(1000, h) * 4 ** (1000 - h), 5**1000)) for h in range(1001)]
    return {
        "monthly_cost_usd": FiniteLaw([(600 + Fraction(7, 5) * h, p) for h, p in states]),
        "success_rate": FiniteLaw([(1 - Fraction(h, 1000), p) for h, p in states]),
        "p95_latency_ms": FiniteLaw([(80 if h <= 50 else 800, p) for h, p in states]),
        "critical_error_rate": FiniteLaw([(Fraction(h, 1000), p) for h, p in states]),
        "quality_drop": FiniteLaw([(Fraction(h, 1000), p) for h, p in states]),
        "loss": FiniteLaw([(-400 + Fraction(1497, 5) * h, p) for h, p in states]),
        "incumbent_cost": FiniteLaw([(1000 + 2 * h, p) for h, p in states]),
    }


@lru_cache(maxsize=3)
def oracle(world):
    laws = true_laws(world)
    probabilities = {
        "success_rate": laws["quality_drop"].breach_probability(Fraction(1, 20)),
        "p95_latency_ms": laws["p95_latency_ms"].breach_probability(200),
        "critical_error_rate": laws["critical_error_rate"].breach_probability(Fraction(1, 20)),
    }
    cvar = laws["loss"].cvar(Fraction(19, 20))
    feasible = all(value <= Fraction(1, 20) for value in probabilities.values()) and cvar <= 0
    incumbent_cost, candidate_cost = laws["incumbent_cost"].mean(), laws[METRICS[0]].mean()
    choice = "candidate" if feasible and candidate_cost < incumbent_cost else "hold"
    return {
        "candidate_feasible": feasible,
        "choice": choice,
        "optimal_cost": float(candidate_cost if choice == "candidate" else incumbent_cost),
        "incumbent_cost": float(incumbent_cost),
        "candidate_cost": float(candidate_cost),
        "breach_probabilities": {key: float(value) for key, value in probabilities.items()},
        "constraint_excess": {
            **{key: max(0, float(value - Fraction(1, 20))) for key, value in probabilities.items()},
            "cvar_loss": max(0, float(cvar)),
        },
    }


def baseline_choice(pairs):
    stats = monthly(pairs)
    return (
        "candidate"
        if (
            stats["savings"] > 0
            and stats["quality_drop"] <= 0.05
            and stats["candidate"]["p95_latency_ms"] <= 200
            and stats["candidate"]["critical_error_rate"] <= 0.05
            and stats["loss"] <= 0
        )
        else "hold"
    )


def wilson(successes, total):
    if not 0 <= successes <= total:
        raise ValueError("invalid binomial count")
    if not total:
        return None
    z = 1.959963984540054
    z2 = z * z
    center = (successes / total + z2 / (2 * total)) / (1 + z2 / total)
    radius = (
        z
        * math.sqrt(successes / total * (1 - successes / total) / total + z2 / (4 * total * total))
        / (1 + z2 / total)
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def episode_unsafe(values):
    if any(value is True for value in values):
        return True
    return None if any(value is None for value in values) else False


def bootstrap_mean(values, seed):
    available = [value for value in values if value is not None]
    if not available:
        return {"mean": None, "ci95": None, "available": 0, "undefined_resamples": 2000}
    rng = random.Random(seed)
    means = []
    missing = 0
    for _ in range(2000):
        sample = [value for value in rng.choices(values, k=len(values)) if value is not None]
        if sample:
            means.append(fmean(sample))
        else:
            missing += 1
    return {
        "mean": fmean(available),
        "ci95": None if missing else [quantile(means, 0.025), quantile(means, 0.975)],
        "available": len(available),
        "undefined_resamples": missing,
    }


def independent_effect(control, treatment, seed):
    if not control or not treatment:
        return {
            "status": "failed",
            "missing_reason": "empty_arm",
            "estimate": None,
            "low": None,
            "high": None,
        }
    rng = random.Random(seed)
    boot = [
        fmean(rng.choices(treatment, k=len(treatment)))
        - fmean(rng.choices(control, k=len(control)))
        for _ in range(2000)
    ]
    return {
        "status": "estimated",
        "estimate": fmean(treatment) - fmean(control),
        "low": quantile(boot, 0.025),
        "high": quantile(boot, 0.975),
    }
