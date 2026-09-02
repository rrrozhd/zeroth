"""Preregistered candidate-sampler checks against independently specified laws."""

import hashlib
import json
import math
from collections import Counter
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from release.economic_evaluation.test_invariants import evidence, policy
from zeroth.econ import probabilistic as candidate

AMENDMENT_HASH = "0e543c2718e518e9655f3b1a65c23dd041bd720ccceb33638d56f44a9f651eac"


def manifest():
    return json.loads(Path(__file__).with_name("numerical_manifest_v1.json").read_text())


def check_frequencies(observations, law_name):
    spec = manifest()
    law = spec["laws"][law_name]
    count = spec["draws_per_seed"]
    tolerance = math.sqrt(math.log(2 * spec["total_atom_checks_M"] / 0.01) / (2 * count))
    counts = Counter(observations)
    checks = [
        {
            "atom": atom,
            "count": counts[atom],
            "expected_probability": probability,
            "frequency": counts[atom] / count,
            "absolute_error": abs(counts[atom] / count - float(Fraction(probability))),
        }
        for atom, probability in zip(law["atoms"], law["probabilities"], strict=True)
    ]
    return {
        "passed": len(observations) == count
        and set(counts) <= set(law["atoms"])
        and all(check["absolute_error"] <= tolerance for check in checks),
        "observations": len(observations),
        "tolerance": tolerance,
        "checks": checks,
        "unexpected_atoms": sorted(set(counts) - set(law["atoms"])),
    }


def sample_candidate(law_name, seed, count):
    """Observe raw candidate scenarios without changing any sampling calculation."""
    observations = []
    if law_name == "K03_savings":
        world = evidence(3)
        world.period_request_counts = [1000, 2000]
        original = candidate.empirical_var_cvar

        def capture(losses, *, confidence):
            observations.extend(-loss for loss in losses)
            return original(losses, confidence=confidence)

        with patch.object(candidate, "empirical_var_cvar", capture):
            candidate.recommend_model_migration(
                world, policy=policy(), simulations=count, seed=seed
            )
    elif law_name == "finite_breach":
        world = evidence(1)
        # Approved v2 adapter: one future request keeps the frozen Bernoulli .04 law.
        world.period_request_counts = [1]
        world.candidate[0].latency_ms = 200
        # Declared one-unit law, not evidence of real-world safety. A nonzero
        # critical indicator avoids the zero-observation evidence gate here.
        world.candidate[0].critical_error = True
        original = candidate._counted_outcomes

        def capture(rows, counts, demand):
            result = original(rows, counts, demand)
            observations.append(int(result[3] > 150))
            return result

        with patch.object(candidate, "_counted_outcomes", capture):
            candidate.recommend_model_migration(
                world,
                policy=policy(candidate_shares=[0.04], max_p95_latency_ms=150),
                simulations=count,
                seed=seed,
            )
    else:
        raise ValueError("unknown preregistered law")
    return observations


def run():
    amendment = Path(__file__).with_name("nested_adapter_amendment_v1.md").read_bytes()
    if hashlib.sha256(amendment).hexdigest() != AMENDMENT_HASH:
        raise ValueError("nested adapter amendment differs from independent approval")
    spec = manifest()
    results = []
    squared_error = {"prefix": 0.0, "full": 0.0}
    for law_name, law in spec["laws"].items():
        for seed in spec["seeds"]:
            observations = sample_candidate(law_name, seed, spec["draws_per_seed"])
            result = check_frequencies(observations, law_name)
            result.update(law=law_name, seed=seed)
            prefix = observations[: spec["prefix_draws"]]
            prefix_counts = Counter(prefix)
            result["prefix_counts"] = {str(atom): prefix_counts[atom] for atom in law["atoms"]}
            for atom, probability in zip(law["atoms"], law["probabilities"], strict=True):
                squared_error["prefix"] += (
                    prefix_counts[atom] / len(prefix) - float(Fraction(probability))
                ) ** 2
            squared_error["full"] += sum(check["absolute_error"] ** 2 for check in result["checks"])
            results.append(result)
    precision_passed = squared_error["full"] < squared_error["prefix"]
    checks = sum(len(result["checks"]) for result in results)
    return {
        "adapter_version": "numerical-v2-nested",
        "adapter_amendment_sha256": AMENDMENT_HASH,
        "forecast_algorithm_version": candidate.FORECAST_ALGORITHM_VERSION,
        "passed": all(result["passed"] for result in results)
        and precision_passed
        and checks == spec["total_atom_checks_M"],
        "atom_checks": checks,
        "aggregate_squared_error": squared_error,
        "aggregate_precision_passed": precision_passed,
        "results": results,
        "scope": "numerical sampling only; not predictive validity",
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
