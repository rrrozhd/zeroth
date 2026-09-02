"""Deterministic experiment integrity, separate from descriptive performance scores."""

import math

from release.economic_evaluation.predictive_reference import METRICS


def metric_errors(row):
    errors = []
    values = [row.get(key) for key in ("predicted_mean", "low", "high")]
    if any(value is None for value in values):
        if not row.get("missing_reason"):
            errors.append("unavailable forecast missing reason")
    elif not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        errors.append("nonfinite forecast")
    elif values[1] > values[2]:
        errors.append("unordered interval")
    return errors


def nonfinite_paths(value, path="rows"):
    if isinstance(value, float) and not math.isfinite(value):
        return [path]
    if isinstance(value, dict):
        return [
            item for key, child in value.items() for item in nonfinite_paths(child, f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in nonfinite_paths(child, f"{path}[{index}]")
        ]
    return []


def summary_errors(summaries):
    expected = set()
    for source in ("candidate", "true_law", "biased", "overconfident"):
        for world in range(3):
            for horizon in range(5):
                for metric in METRICS:
                    for kind in (
                        "bias",
                        "width",
                        "coverage" if horizon < 4 else "episode_mean_coverage",
                    ):
                        expected.add((source, world, horizon, metric, kind))
                    if metric != "monthly_cost_usd":
                        expected.add((source, world, horizon, metric, "probability_absolute_error"))
                if source == "candidate":
                    for kind in (
                        ("public_safety_availability", "feasible_regret")
                        if horizon < 4
                        else ("episode_any_unsafe", "episode_feasible_regret")
                    ):
                        expected.add((source, world, horizon, None, kind))
    actual = [
        tuple(row.get(key) for key in ("source", "world", "horizon", "metric", "kind"))
        for row in summaries
    ]
    errors = [f"summary nonfinite: {path}" for path in nonfinite_paths(summaries)]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        errors.append("forecast summaries: missing, duplicate or incorrect IDs")
    bootstrap_fields = {"mean", "ci95", "available", "undefined_resamples"}
    envelopes = {
        "bias": bootstrap_fields,
        "width": bootstrap_fields,
        "probability_absolute_error": bootstrap_fields | {"status"},
        "feasible_regret": bootstrap_fields,
        "episode_feasible_regret": bootstrap_fields | {"available_horizons"},
        "episode_mean_coverage": bootstrap_fields | {"episode_values", "available_horizons"},
        "coverage": {"numerator", "denominator", "missing", "rate", "ci95"},
        "episode_any_unsafe": {
            "values",
            "observed_event_rate_lower_bound",
            "unknown",
            "incomplete",
        },
        "public_safety_availability": {
            "total",
            "full_candidate_recommendations",
            "unsafe",
            "conditional_unsafe_rate",
            "conditional_ci95",
            "unconditional_unsafe_rate",
            "unconditional_ci95",
            "choice_counts",
            "exceptions",
            "structural_abstention_complete",
            "structural_abstention_incomplete",
        },
    }
    for row in summaries:
        kind = row.get("kind")
        if not envelopes.get(kind, {"unknown_kind"}) <= row.keys():
            errors.append(f"forecast summary {kind}: incomplete result envelope")
        if (
            kind in ("episode_mean_coverage", "episode_feasible_regret")
            and len(row.get("available_horizons", [])) != 30
        ):
            errors.append(f"forecast summary {kind}: missing episode availability")
        if kind in (
            "bias",
            "width",
            "probability_absolute_error",
            "feasible_regret",
            "episode_feasible_regret",
            "episode_mean_coverage",
        ):
            available = row.get("available")
            if not isinstance(available, int) or not 0 <= available <= 30:
                errors.append(f"forecast summary {kind}: invalid availability")
            if row.get("mean") is None and available != 0:
                errors.append(f"forecast summary {kind}: missing mean without unavailability")
            if row.get("ci95") is None and not row.get("undefined_resamples"):
                errors.append(f"forecast summary {kind}: undefined interval without reason")
        for field in ("ci95", "conditional_ci95", "unconditional_ci95"):
            interval = row.get(field)
            if interval is not None and (len(interval) != 2 or interval[0] > interval[1]):
                errors.append(f"forecast summary {kind}: unordered {field}")
    return errors


def validate(rows, summaries=None):
    errors = [f"nonfinite: {path}" for path in nonfinite_paths(rows)]
    errors.extend(summary_errors(summaries or []))

    def check_keys(name, fields, expected):
        observed = [tuple(row.get(field) for field in fields) for row in rows.get(name, [])]
        if len(observed) != len(set(observed)):
            errors.append(f"{name}: duplicate IDs")
        if set(observed) != expected:
            errors.append(f"{name}: missing/incorrect IDs or row fields")

    holdout = {(w, r, h) for w in range(3) for r in range(30) for h in range(4)}
    historical = {(w, r, t) for w in range(3) for r in range(30) for t in range(6, 12)}
    check_keys("decisions", ("world", "replication", "horizon"), holdout)
    check_keys("future_truth", ("world", "replication", "horizon"), holdout)
    check_keys(
        "history_evidence", ("world", "replication"), {(w, r) for w in range(3) for r in range(30)}
    )
    check_keys("historical_forecasts", ("world", "replication", "origin_month"), historical)
    check_keys(
        "metrics",
        ("world", "replication", "horizon", "metric"),
        {(*key, metric) for key in holdout for metric in METRICS},
    )
    check_keys(
        "historical_metrics",
        ("world", "replication", "origin_month", "metric"),
        {(*key, metric) for key in historical for metric in METRICS},
    )
    check_keys(
        "controls",
        ("world", "replication", "horizon", "metric", "source"),
        {
            (*key, metric, control)
            for key in holdout
            for metric in METRICS
            for control in ("true_law", "biased", "overconfident")
        },
    )
    check_keys(
        "baseline_actions",
        ("world", "replication", "horizon", "baseline"),
        {(*key, label) for key in holdout for label in ("always_hold", "measured_deterministic")},
    )
    check_keys(
        "causal_replicates", ("world", "replication"), {(w, r) for w in (3, 4) for r in range(30)}
    )
    check_keys(
        "causal_summaries",
        ("world", "estimator", "kind"),
        {
            (w, estimator, kind)
            for w in (3, 4)
            for estimator in ("independent", "production")
            for kind in (
                "mean_bias",
                "coverage",
                "null_rejection",
                "effect_rejection_power",
                "availability_status",
            )
        },
    )
    targets = {
        (row.get("world"), row.get("replication"), row.get("horizon")): row
        for row in rows.get("future_truth", [])
    }
    for name in ("decisions", "metrics", "controls", "baseline_actions"):
        for row in rows.get(name, []):
            horizon = row.get("horizon")
            if (
                not isinstance(horizon, int)
                or row.get("origin_month") != 12
                or row.get("target_month") != 12 + horizon
                or row.get("fit_months") != [9, 10, 11]
            ):
                errors.append(f"{name}: wrong fixed origin, horizon or fit window")
            if name in ("metrics", "controls"):
                errors.extend(f"{name}: {error}" for error in metric_errors(row))
                truth = targets.get((row.get("world"), row.get("replication"), horizon), {})
                actual = truth.get("outcomes", {}).get("candidate", {}).get(row.get("metric"))
                if actual is None or row.get("observed") != actual:
                    errors.append(f"{name}: wrong or missing target truth")
                if (
                    name == "metrics"
                    and row.get("predicted_mean") is not None
                    and row.get("action_id") != "global-1"
                ):
                    errors.append("metrics: wrong full-candidate action identity")
    for name in ("historical_forecasts", "decisions"):
        for row in rows.get(name, []):
            origin = row.get("origin_month")
            months = row.get("fit_months", [])
            if not isinstance(origin, int) or not months or any(m >= origin for m in months):
                errors.append(f"{name}: input includes target/future month")
            expected_seed = (
                1000000
                + 100000 * row.get("world", -100)
                + 1000 * row.get("replication", -100)
                + (
                    100 + origin
                    if name == "historical_forecasts" and isinstance(origin, int)
                    else 200 + row.get("horizon", -100)
                )
            )
            if row.get("seed") != expected_seed:
                errors.append(f"{name}: wrong candidate RNG stream")
            lineage = row.get("input_lineage", {})
            ids = lineage.get("case_ids", [])
            if len(ids) != 300 or len(set(ids)) != 300 or lineage.get("demand_horizon") != "month":
                errors.append(f"{name}: missing/duplicated input units or horizon")
            expected_ids = {
                f"{row.get('world')}/{row.get('replication')}/{month}/{unit}"
                for month in months
                for unit in range(100)
            }
            if set(ids) != expected_ids:
                errors.append(f"{name}: input unit lineage outside fit window")
            if row.get("report") is None and not (row.get("exception") or row.get("error")):
                errors.append(f"{name}: absent result lacks exception reason")
            if name == "historical_forecasts" and row.get("target_month") != origin:
                errors.append("historical forecast targets wrong month")
    for row in rows.get("historical_metrics", []):
        errors.extend(f"historical_metrics: {error}" for error in metric_errors(row))
    for row in rows.get("causal_replicates", []):
        truth = 0.0 if row.get("world") == 3 else -0.2
        for estimator in ("independent", "production"):
            block = row.get(estimator, {})
            if not block.get("status") or block.get("truth") != truth:
                errors.append(f"causal {estimator}: missing status or wrong ITT truth")
            if block.get("estimate") is None and not block.get("missing_reason"):
                errors.append(f"causal {estimator}: missing estimate without reason")
        units = row.get("raw_units", [])
        if len(units) != 1000 or len({unit.get("subject_id") for unit in units}) != 1000:
            errors.append("causal: missing or duplicate experimental units")
        for unit in units:
            assigned = unit.get("assignment")
            if assigned not in (0, 1) or unit.get("Y0") not in (1, 2):
                errors.append("causal: invalid assignment or latent outcome")
                continue
            if abs(unit.get("Y1", 999) - unit["Y0"] - truth) > 1e-9 or unit.get(
                "observed"
            ) != unit.get("Y1" if assigned else "Y0"):
                errors.append("causal: observed/potential outcome mismatch")
    return {"passed": not errors, "errors": errors}
