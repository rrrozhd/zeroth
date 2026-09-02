"""Approved v3 descriptive experiment. No activation or performance pass threshold."""

import argparse
import json
import random
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from release.economic_evaluation.cli import CONTRACT, ROOT, identity, sha
from release.economic_evaluation.predictive_reference import (
    METRICS,
    baseline_choice,
    bootstrap_mean,
    demand,
    episode_unsafe,
    generate_pairs,
    independent_effect,
    monthly,
    oracle,
    true_laws,
    wilson,
)
from zeroth.econ import probabilistic as candidate
from zeroth.econ import rollout_verification as causal_candidate

MANIFEST_HASH = "d459fa29237905fa5d90f0948a9d4190a746220bfc5051bc88aa1c69fa28ed74"
SUPPLEMENT_HASH = "b3ddcd402c56259c4a18d7dc9e2fe6dc74cebf2008c1d02b67d01e460dbe8cd4"
AMENDMENT_HASH = "0e543c2718e518e9655f3b1a65c23dd041bd720ccceb33638d56f44a9f651eac"
INVENTORY = {
    "decisions": 360,
    "baseline_actions": 720,
    "metrics": 1440,
    "controls": 4320,
    "historical_forecasts": 540,
    "historical_metrics": 2160,
    "causal_replicates": 60,
    "causal_summaries": 20,
}
ATTRIBUTES = {
    "monthly_cost_usd": (
        "expected_monthly_cost_usd",
        "monthly_cost_p05_usd",
        "monthly_cost_p95_usd",
        None,
    ),
    "success_rate": (
        "expected_success_rate",
        "success_rate_p05",
        "success_rate_p95",
        "probability_quality_breach",
    ),
    "p95_latency_ms": (
        "expected_p95_latency_ms",
        "p95_latency_p05_ms",
        "p95_latency_p95_ms",
        "probability_latency_breach",
    ),
    "critical_error_rate": (
        "expected_critical_error_rate",
        "critical_error_rate_p05",
        "critical_error_rate_p95",
        "probability_critical_error_breach",
    ),
}


def month_start(index):
    return datetime(2025 + index // 12, index % 12 + 1, 1, tzinfo=UTC)


def check_inventory(rows):
    actual = {name: len(rows.get(name, [])) for name in INVENTORY}
    return {"passed": actual == INVENTORY, "expected": INVENTORY, "actual": actual}


def rules(historical):
    return candidate.MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1],
        routing_actions=[],
        max_quality_drop=0.05,
        max_p95_latency_ms=200,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.05,
        cvar_confidence=0.95,
        max_cvar_loss_usd="0",
        critical_error_penalty_usd="300",
        allow_drift_warning=False,
        require_calibrated_forecast=not historical,
    )


def fit_evidence(world, replication, history, fit_months, readiness):
    arms = [[], []]
    for month in fit_months:
        for unit, pair in enumerate(history[month]["measurement"]):
            for arm in (0, 1):
                cost, latency, accepted, critical = pair[arm]
                arms[arm].append(
                    candidate.MigrationObservation(
                        case_id=f"{world}/{replication}/{month}/{unit}",
                        cohort="default",
                        cost_usd=str(cost),
                        latency_ms=latency,
                        accepted=accepted,
                        critical_error=critical,
                        source="synthetic-v3",
                    )
                )
    return candidate.MigrationEvidence(
        workload="synthetic-v3",
        incumbent_model="incumbent",
        candidate_model="candidate",
        incumbent=arms[0],
        candidate=arms[1],
        period_request_counts=[history[month]["demand"] for month in fit_months],
        demand_horizon="month",
        readiness=readiness,
    )


def predict(world, replication, history, fit_months, readiness, seed, historical, errors, row_id):
    try:
        evidence = fit_evidence(world, replication, history, fit_months, readiness)
        if any(month >= row_id["origin_month"] for month in fit_months):
            raise ValueError("forecast inputs include target/future month")
        row_id["input_lineage"] = {
            "case_ids": [row.case_id for row in evidence.incumbent],
            "demand_horizon": evidence.demand_horizon,
            "evidence_sha256": sha(evidence.model_dump_json().encode()),
        }
        result = candidate.recommend_model_migration(
            evidence, policy=rules(historical), simulations=1000, seed=seed
        )
        return result.model_dump(mode="json"), None
    except Exception:
        failure = {"row_id": row_id, "traceback": traceback.format_exc()}
        errors.append(failure)
        return None, failure["traceback"]


def metric_row(
    context,
    metric,
    mean,
    low,
    high,
    observed,
    probability=None,
    truth_probability=None,
    missing=None,
):
    present = all(value is not None for value in (mean, low, high))
    return {
        **context,
        "metric": metric,
        "predicted_mean": mean,
        "low": low,
        "high": high,
        "observed": observed,
        "bias": mean - observed if present else None,
        "coverage": low <= observed <= high if present else None,
        "width": high - low if present else None,
        "breach_probability": probability,
        "true_breach_probability": truth_probability,
        "probability_absolute_error": abs(probability - truth_probability)
        if probability is not None and truth_probability is not None
        else None,
        "missing_reason": missing if not present else None,
    }


def forecast_metrics(context, report, target, world):
    action = next(
        (
            a
            for a in (report or {}).get("actions", [])
            if a["candidate_share"] == 1 and not a.get("cohort_candidate_shares")
        ),
        None,
    )
    missing = ",".join((report or {}).get("reason_codes", [])) or "forecast_exception_or_absent"
    rows = []
    for metric, attributes in ATTRIBUTES.items():
        values = [
            float(action[name]) if action and name and action.get(name) is not None else None
            for name in attributes
        ]
        rows.append(
            metric_row(
                {
                    **context,
                    "source": "candidate_experimental_full_action",
                    "action_id": action["action_id"] if action else None,
                },
                metric,
                *values[:3],
                target["candidate"][metric],
                probability=values[3],
                truth_probability=oracle(world)["breach_probabilities"].get(metric),
                missing=missing,
            )
        )
    return rows


def action_row(context, choice, world, exception=False):
    truth = oracle(world)
    unsafe = choice == "candidate" and not truth["candidate_feasible"]
    feasible = choice == "hold" or (choice == "candidate" and truth["candidate_feasible"])
    cost = truth["candidate_cost"] if choice == "candidate" else truth["incumbent_cost"]
    return {
        **context,
        "choice": choice,
        "exception": exception,
        "unsafe": None if exception else unsafe,
        "truth_feasible": feasible if choice in {"candidate", "hold"} else None,
        "feasible_regret": cost - truth["optimal_cost"] if feasible else None,
        "constraint_excess": truth["constraint_excess"] if unsafe else None,
    }


def control_rows(context, world, target):
    rows = []
    for metric in METRICS:
        law = true_laws(world)[metric]
        low, high, mean = (
            float(law.quantile("1/20")),
            float(law.quantile("19/20")),
            float(law.mean()),
        )
        midpoint, delta = (low + high) / 2, 0.25 * max(abs(mean), 1)
        controls = [
            ("true_law", mean, low, high, None),
            ("biased", mean + delta, low + delta, high + delta, None),
            (
                "overconfident",
                midpoint,
                midpoint - 0.05 * (high - low),
                midpoint + 0.05 * (high - low),
                None,
            ),
        ]
        if low == high:
            controls[-1] = ("overconfident", None, None, None, "not_applicable_zero_true_width")
        for label, predicted, lower, upper, reason in controls:
            row = metric_row(
                {**context, "source": label},
                metric,
                predicted,
                lower,
                upper,
                target["candidate"][metric],
                missing=reason,
            )
            row["exact_control_coverage"] = (
                float(sum(mass for atom, mass in law.atoms if lower <= float(atom) <= upper))
                if lower is not None
                else None
            )
            rows.append(row)
    return rows


def predictive_replication(world, replication, rows, errors):
    base = 1000000 + 100000 * world + 1000 * replication
    rng = {index: random.Random(base + index) for index in range(5)}
    history = []
    for _month in range(12):
        measured = generate_pairs(world, 100, rng[0])
        volume = demand(world, rng[1])
        target_pairs = generate_pairs(world, volume, rng[2])
        history.append(
            {
                "measurement": measured,
                "demand": volume,
                "target": monthly(target_pairs),
                "target_hard_count": sum(pair[1][3] for pair in target_pairs),
            }
        )
    rows.setdefault("history_evidence", []).append(
        {"world": world, "replication": replication, "months": history}
    )
    calibration = []
    for origin in range(6, 12):
        context = {
            "world": world,
            "replication": replication,
            "origin_month": origin,
            "target_month": origin,
            "fit_months": list(range(origin - 3, origin)),
            "seed": base + 100 + origin,
        }
        report, failure = predict(
            world,
            replication,
            history[:origin],
            context["fit_months"],
            candidate.ForecastReadiness(),
            context["seed"],
            True,
            errors,
            context,
        )
        rows["historical_forecasts"].append({**context, "report": report, "exception": failure})
        metrics = forecast_metrics(context, report, history[origin]["target"], world)
        rows["historical_metrics"].extend(metrics)
        for row in metrics:
            if row["predicted_mean"] is not None:
                calibration.append(
                    candidate.ForecastCalibrationObservation(
                        forecast_id=f"{world}/{replication}/{origin}",
                        metric=row["metric"],
                        predicted_mean=row["predicted_mean"],
                        predicted_low=row["low"],
                        predicted_high=row["high"],
                        observed=row["observed"],
                        observed_at=month_start(origin + 1) - timedelta(microseconds=1),
                    )
                )
    complete = len(calibration) == 24
    readiness = (
        candidate.assess_forecast_readiness(
            calibration,
            required_metrics=set(METRICS),
            minimum_periods=6,
            minimum_interval_coverage=0.9,
            max_relative_bias=0.1,
            max_relative_residual_shift=0.2,
        )
        if complete
        else candidate.ForecastReadiness(missing_metrics=list(METRICS))
    )
    measurements = [pair for month in (9, 10, 11) for pair in history[month]["measurement"]]
    measured_baseline = baseline_choice(measurements)
    for horizon in range(4):
        context = {
            "world": world,
            "replication": replication,
            "horizon": horizon,
            "origin_month": 12,
            "target_month": 12 + horizon,
            "fit_months": [9, 10, 11],
            "seed": base + 200 + horizon,
        }
        report, failure = predict(
            world,
            replication,
            history,
            [9, 10, 11],
            readiness,
            context["seed"],
            False,
            errors,
            context,
        )
        # Reveal independently generated future truth only AFTER the forecast call.
        volume = demand(world, rng[3])
        target_pairs = generate_pairs(world, volume, rng[4])
        target = monthly(target_pairs)
        rows.setdefault("future_truth", []).append(
            {
                **context,
                "demand": volume,
                "hard_count": sum(pair[1][3] for pair in target_pairs),
                "outcomes": target,
            }
        )
        public = (report or {}).get("recommended_action")
        choice = (
            "candidate"
            if public == "ship_candidate"
            else "hold"
            if public == "keep_incumbent"
            else "abstain"
            if report
            else None
        )
        rows["decisions"].append(
            {
                **action_row(context, choice, world, bool(failure)),
                "report": report,
                "error": failure,
                "structural_evidence_complete": complete,
                "abstention_with_complete_structural_evidence": choice == "abstain" and complete,
                "abstention_with_incomplete_structural_evidence": choice == "abstain"
                and not complete,
                "nonabstention_with_incomplete_structural_evidence": choice in {"candidate", "hold"}
                and not complete,
            }
        )
        for label, baseline in (
            ("always_hold", "hold"),
            ("measured_deterministic", measured_baseline),
        ):
            rows["baseline_actions"].append(
                action_row({**context, "baseline": label}, baseline, world)
            )
        rows["metrics"].extend(forecast_metrics(context, report, target, world))
        rows["controls"].extend(control_rows(context, world, target))


def score_effect(block, truth):
    estimate, low, high = block.get("estimate"), block.get("low"), block.get("high")
    interval = low is not None and high is not None
    return {
        **block,
        "truth": truth,
        "bias": estimate - truth if estimate is not None else None,
        "coverage": low <= truth <= high if interval else None,
        "rejected_zero": (low > 0 or high < 0) if interval else None,
    }


def causal_replication(world, replication, rows, errors):
    base = 1000000 + 100000 * world + 1000 * replication
    assignments_rng, outcomes_rng = random.Random(base + 10), random.Random(base + 11)
    truth = 0.0 if world == 3 else -0.2
    assignments, observations, control, treatment = [], [], [], []
    raw = []
    for unit in range(1000):
        assigned = assignments_rng.random() < 0.5
        baseline = 1.0 if outcomes_rng.random() < 0.5 else 2.0
        observed = baseline + truth if assigned else baseline
        (treatment if assigned else control).append(observed)
        name = "candidate" if assigned else "incumbent"
        subject = f"{world}/{replication}/{unit}"
        assignments.append(
            causal_candidate.RolloutAssignment(
                subject_id=subject,
                arm=name,
                assigned_model=name,
                assigned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        observations.append(
            causal_candidate.RolloutObservation(
                subject_id=subject,
                model_used=name,
                cost_usd=str(observed),
                latency_ms=100,
                accepted=True,
                critical_error=False,
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        raw.append(
            {
                "subject_id": subject,
                "assignment": int(assigned),
                "Y0": baseline,
                "Y1": baseline + truth,
                "observed": observed,
            }
        )
    independent = independent_effect(control, treatment, base + 12)
    plan = causal_candidate.RandomizedRolloutPlan(
        rollout_id=f"{world}/{replication}",
        workload="synthetic-v3",
        incumbent_model="incumbent",
        candidate_model="candidate",
        candidate_probability=0.5,
        assignment_salt="diagnostic-v3-not-secret",
    )
    try:
        report = causal_candidate.verify_randomized_rollout(
            plan,
            assignments,
            observations,
            minimum_per_arm=100,
            bootstrap_samples=2000,
            seed=base + 13,
            verified_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        effect = report.effects.get("cost_usd")
        production = {
            "status": report.causal_status,
            "reason_codes": report.reason_codes,
            "estimate": effect.estimated_difference if effect else None,
            "low": effect.confidence_low if effect else None,
            "high": effect.confidence_high if effect else None,
            "missing_reason": None if effect else "production_effect_unavailable",
            "report": report.model_dump(mode="json"),
        }
    except Exception:
        error = {"world": world, "replication": replication, "traceback": traceback.format_exc()}
        errors.append(error)
        production = {
            "status": "exception",
            "estimate": None,
            "low": None,
            "high": None,
            "missing_reason": error["traceback"],
        }
    rows["causal_replicates"].append(
        {
            "world": world,
            "replication": replication,
            "control_n": len(control),
            "treatment_n": len(treatment),
            "raw_units": raw,
            "independent": score_effect(independent, truth),
            "production": score_effect(production, truth),
        }
    )


def causal_summaries(rows):
    for world in (3, 4):
        replicates = [row for row in rows["causal_replicates"] if row["world"] == world]
        for estimator in ("independent", "production"):
            blocks = [row[estimator] for row in replicates]
            available_estimates = sum(block["estimate"] is not None for block in blocks)
            available_intervals = sum(block["coverage"] is not None for block in blocks)
            common = {
                "world": world,
                "estimator": estimator,
                "total": 30,
                "available_estimates": available_estimates,
                "available_intervals": available_intervals,
                "exceptions": sum(block["status"] == "exception" for block in blocks),
                "unavailable": 30 - available_intervals,
                "status_counts": dict(Counter(block["status"] for block in blocks)),
            }
            # Identical seed intentionally gives the paired replication-index resamples.
            summary = bootstrap_mean(
                [block["bias"] for block in blocks], 9000000 + 10000 * world + 500 + 12
            )
            rows["causal_summaries"].append({**common, "kind": "mean_bias", **summary})
            for kind, field, applicable in (
                ("coverage", "coverage", True),
                ("null_rejection", "rejected_zero", world == 3),
                ("effect_rejection_power", "rejected_zero", world == 4),
            ):
                successes = sum(block[field] is True for block in blocks) if applicable else None
                denominator = sum(block[field] is not None for block in blocks) if applicable else 0
                rows["causal_summaries"].append(
                    {
                        **common,
                        "kind": kind,
                        "status": "descriptive" if applicable else "not_applicable",
                        "numerator": successes,
                        "denominator": denominator,
                        "rate": successes / denominator if denominator else None,
                        "ci95": wilson(successes, denominator) if applicable else None,
                    }
                )
            rows["causal_summaries"].append({**common, "kind": "availability_status"})


def forecast_summaries(rows, source="candidate"):
    summaries = []
    metric_rows = (
        rows["metrics"]
        if source == "candidate"
        else [row for row in rows["controls"] if row["source"] == source]
    )
    for world in range(3):
        for horizon in range(5):
            for index, metric in enumerate(METRICS):
                groups = [
                    [
                        row
                        for row in metric_rows
                        if row["world"] == world
                        and row["replication"] == replication
                        and row["metric"] == metric
                        and (horizon == 4 or row["horizon"] == horizon)
                    ]
                    for replication in range(30)
                ]
                for field, metric_index in (("bias", 2 * index), ("width", 2 * index + 1)):
                    values = [
                        sum(valid) / len(valid)
                        if (valid := [row[field] for row in group if row[field] is not None])
                        else None
                        for group in groups
                    ]
                    summaries.append(
                        {
                            "world": world,
                            "horizon": horizon,
                            "metric": metric,
                            "kind": field,
                            "total_replications": 30,
                            "intervals": "exploratory_unadjusted",
                            **bootstrap_mean(
                                values, 9000000 + 10000 * world + 100 * horizon + metric_index
                            ),
                        }
                    )
                coverages = [
                    sum(valid) / len(valid)
                    if (valid := [row["coverage"] for row in group if row["coverage"] is not None])
                    else None
                    for group in groups
                ]
                if horizon < 4:
                    count = sum(value is not None for value in coverages)
                    success = sum(value == 1 for value in coverages)
                    summaries.append(
                        {
                            "world": world,
                            "horizon": horizon,
                            "metric": metric,
                            "kind": "coverage",
                            "numerator": success,
                            "denominator": count,
                            "missing": 30 - count,
                            "rate": success / count if count else None,
                            "ci95": wilson(success, count),
                        }
                    )
                else:
                    # Episode coverage is a mean over available horizons, NOT Bernoulli.
                    summaries.append(
                        {
                            "world": world,
                            "horizon": horizon,
                            "metric": metric,
                            "kind": "episode_mean_coverage",
                            "episode_values": coverages,
                            "available_horizons": [
                                sum(row["coverage"] is not None for row in group)
                                for group in groups
                            ],
                            "summary_resamples": "paired_with_metric_episode_bias",
                            **bootstrap_mean(
                                coverages, 9000000 + 10000 * world + 100 * horizon + 2 * index
                            ),
                        }
                    )
                if metric != "monthly_cost_usd":
                    values = [
                        sum(valid) / len(valid)
                        if (
                            valid := [
                                row["probability_absolute_error"]
                                for row in group
                                if row["probability_absolute_error"] is not None
                            ]
                        )
                        else None
                        for group in groups
                    ]
                    summaries.append(
                        {
                            "world": world,
                            "horizon": horizon,
                            "metric": metric,
                            "kind": "probability_absolute_error",
                            "status": "descriptive" if source == "candidate" else "not_applicable",
                            **bootstrap_mean(
                                values, 9000000 + 10000 * world + 100 * horizon + 7 + index
                            ),
                        }
                    )
            if source != "candidate":
                continue
            actions = [
                row
                for row in rows["decisions"]
                if row["world"] == world and (horizon == 4 or row["horizon"] == horizon)
            ]
            if horizon < 4:
                recommendations = sum(row["choice"] == "candidate" for row in actions)
                unsafe = sum(row["unsafe"] is True for row in actions)
                summaries.append(
                    {
                        "world": world,
                        "horizon": horizon,
                        "kind": "public_safety_availability",
                        "total": 30,
                        "full_candidate_recommendations": recommendations,
                        "unsafe": unsafe,
                        "conditional_unsafe_rate": unsafe / recommendations
                        if recommendations
                        else None,
                        "conditional_ci95": wilson(unsafe, recommendations),
                        "unconditional_unsafe_rate": unsafe / 30,
                        "unconditional_ci95": wilson(unsafe, 30),
                        "choice_counts": dict(Counter(row["choice"] for row in actions)),
                        "exceptions": sum(row["exception"] for row in actions),
                        "structural_abstention_complete": sum(
                            row["abstention_with_complete_structural_evidence"] for row in actions
                        ),
                        "structural_abstention_incomplete": sum(
                            row["abstention_with_incomplete_structural_evidence"] for row in actions
                        ),
                    }
                )
                regrets = [row["feasible_regret"] for row in actions]
                summaries.append(
                    {
                        "world": world,
                        "horizon": horizon,
                        "kind": "feasible_regret",
                        **bootstrap_mean(regrets, 9000000 + 10000 * world + 100 * horizon + 11),
                    }
                )
            else:
                episodes = [
                    episode_unsafe(
                        [row["unsafe"] for row in actions if row["replication"] == replication]
                    )
                    for replication in range(30)
                ]
                summaries.append(
                    {
                        "world": world,
                        "kind": "episode_any_unsafe",
                        "values": episodes,
                        "horizon": horizon,
                        "observed_event_rate_lower_bound": sum(value is True for value in episodes)
                        / 30,
                        "unknown": sum(value is None for value in episodes),
                        "incomplete": sum(
                            any(row["exception"] for row in actions if row["replication"] == r)
                            for r in range(30)
                        ),
                    }
                )
                regret_groups = [
                    [
                        row["feasible_regret"]
                        for row in actions
                        if row["replication"] == replication and row["feasible_regret"] is not None
                    ]
                    for replication in range(30)
                ]
                regrets = [sum(group) / len(group) if group else None for group in regret_groups]
                summaries.append(
                    {
                        "world": world,
                        "horizon": horizon,
                        "kind": "episode_feasible_regret",
                        "available_horizons": [len(group) for group in regret_groups],
                        **bootstrap_mean(regrets, 9000000 + 10000 * world + 400 + 11),
                    }
                )
    return [{**row, "source": source} for row in summaries]


def run():
    rows = {name: [] for name in INVENTORY}
    errors = []
    for world in range(3):
        for replication in range(30):
            predictive_replication(world, replication, rows, errors)
            print(f"predictive world={world} replication={replication} completed", flush=True)
    for world in (3, 4):
        for replication in range(30):
            causal_replication(world, replication, rows, errors)
            print(f"causal world={world} replication={replication} completed", flush=True)
    causal_summaries(rows)
    return {
        "rows": rows,
        "forecast_summaries": [
            row
            for source in ("candidate", "true_law", "biased", "overconfident")
            for row in forecast_summaries(rows, source)
        ],
        "inventory": check_inventory(rows),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("retain prior experiments; choose a new output path")
    before = identity()
    if before["tracked_diff"] or before["untracked_source_hashes"]:
        parser.error("commit and identify harness, manifest and candidate before execution")
    manifest_path = Path(__file__).with_name("predictive_manifest_proposal_v3.md")
    if sha(manifest_path.read_bytes()) != MANIFEST_HASH:
        parser.error("manifest bytes differ from independent approval")
    supplement_path = Path(__file__).with_name("predictive_manifest_v3_supplement.md")
    if sha(supplement_path.read_bytes()) != SUPPLEMENT_HASH:
        parser.error("supplement bytes differ from independent approval")
    amendment_path = Path(__file__).with_name("nested_adapter_amendment_v1.md")
    if sha(amendment_path.read_bytes()) != AMENDMENT_HASH:
        parser.error("nested adapter amendment differs from independent approval")
    contract_hash = sha((ROOT / "docs/operations/economic-evaluation-contract-v1.md").read_bytes())
    if contract_hash != CONTRACT:
        parser.error("frozen contract identity mismatch")
    started = datetime.now(UTC).isoformat()
    results = run()
    from release.economic_evaluation.predictive_conformance import validate

    conformance = validate(results["rows"], results["forecast_summaries"])
    after = identity()
    stable = all(
        before[key] == after[key]
        for key in ("revision", "tracked_diff_sha256", "untracked_source_identity_sha256")
    )
    completed = (
        results["inventory"]["passed"]
        and conformance["passed"]
        and not results["errors"]
        and stable
    )
    bundle = {
        "manifest_sha256": MANIFEST_HASH,
        "generator_version": "finite-diagnostic-v3",
        "summary_supplement_sha256": SUPPLEMENT_HASH,
        "contract_sha256": contract_hash,
        "fixture_sha256": sha(Path(__file__).with_name("known_answers_v1.json").read_bytes()),
        "command": [
            sys.executable,
            "-m",
            "release.economic_evaluation.predictive",
            "--output",
            str(args.output),
        ],
        "seed_manifest": {
            "base": "1000000+100000*world+1000*replication",
            "predictive_stream_offsets": {
                "measurements": 0,
                "historical_demand": 1,
                "historical_target": 2,
                "future_demand": 3,
                "future_target": 4,
            },
            "historical_forecasts": "base+100+origin_month",
            "holdouts": "base+200+horizon",
            "causal_offsets": {
                "assignment": 10,
                "outcome": 11,
                "independent_bootstrap": 12,
                "production_bootstrap": 13,
            },
            "summaries": "9000000+10000*world+100*horizon+metric_index; paired per supplement",
        },
        "adapter_version": "monthly-paired-v4-nested",
        "adapter_amendment_sha256": AMENDMENT_HASH,
        "forecast_algorithm_version": candidate.FORECAST_ALGORITHM_VERSION,
        "metrics_version": "episode-summary-v3",
        "candidate": before,
        "candidate_after": after,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "candidate_stable": stable,
        "lock_sha256": sha((Path(__file__).resolve().parents[2] / "uv.lock").read_bytes()),
        "harness_completed": completed,
        "exit_code": 0 if completed else 1,
        "conformance": conformance,
        "scope": "descriptive synthetic slice only; no production/customer validation",
        "predictive_validity": "blocked",
        "customer_sufficiency": "blocked",
        "necessary_unnecessary_abstention": "unassessed",
        "deferred_worlds": "unexecuted",
        "aggregate_request_support": (
            "see separate exact support gate in candidate evidence; no automatic predictive pass"
        ),
        **results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "harness_completed": completed,
                "inventory": results["inventory"],
            }
        )
    )
    return bundle["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
