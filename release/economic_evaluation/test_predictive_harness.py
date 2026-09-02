"""Harness mechanics tested without running/tuning predictive experiments."""

import unittest
from fractions import Fraction


class PredictiveHarnessTests(unittest.TestCase):
    def test_generator_truth_and_integer_monthly_totals(self):
        from release.economic_evaluation.predictive_reference import monthly, outcome, true_laws

        easy = outcome(2, False)
        hard = outcome(2, True)
        self.assertEqual(monthly([easy, hard])["candidate"]["monthly_cost_usd"], 2.6)
        self.assertEqual(monthly([easy, hard])["candidate"]["critical_error_rate"], 0.5)
        self.assertEqual(monthly([easy, hard])["candidate"]["p95_latency_ms"], 800)
        self.assertEqual(true_laws(0)["monthly_cost_usd"].mean(), 600)
        self.assertEqual(true_laws(1)["monthly_cost_usd"].mean(), 900)
        self.assertEqual(true_laws(2)["monthly_cost_usd"].mean(), 880)
        self.assertEqual(true_laws(2)["critical_error_rate"].mean(), Fraction(1, 5))

    def test_unknown_episode_is_not_zero_unsafe(self):
        from release.economic_evaluation.predictive import episode_unsafe

        self.assertIsNone(episode_unsafe([False, False, None, False]))
        self.assertIs(episode_unsafe([False, True, None, False]), True)
        self.assertIs(episode_unsafe([False] * 4), False)

    def test_zero_recommendations_has_undefined_conditional_safety(self):
        from release.economic_evaluation.predictive import wilson

        self.assertIsNone(wilson(0, 0))
        self.assertEqual(wilson(0, 30)[0], 0)
        self.assertAlmostEqual(wilson(0, 30)[1], 0.11351339317396876)
        with self.assertRaises(ValueError):
            wilson(2, 1)

    def test_inventory_does_not_drop_missing_rows(self):
        from release.economic_evaluation.predictive import INVENTORY, check_inventory

        rows = {name: [{}] * count for name, count in INVENTORY.items()}
        self.assertTrue(check_inventory(rows)["passed"])
        rows["causal_summaries"].pop()
        self.assertFalse(check_inventory(rows)["passed"])

    def test_count_only_placeholders_fail_conformance(self):
        from release.economic_evaluation.predictive import INVENTORY
        from release.economic_evaluation.predictive_conformance import validate

        self.assertFalse(
            validate({name: [{}] * count for name, count in INVENTORY.items()})["passed"]
        )

    def test_metric_row_rejects_nonfinite_and_missing_reason(self):
        from release.economic_evaluation.predictive_conformance import metric_errors

        row = {
            "predicted_mean": None,
            "low": None,
            "high": None,
            "observed": 0,
            "missing_reason": None,
        }
        self.assertTrue(metric_errors(row))
        row["missing_reason"] = "forecast_unavailable"
        self.assertFalse(metric_errors(row))
        row.update(predicted_mean=float("nan"), low=0, high=1)
        self.assertTrue(metric_errors(row))

    def test_summary_ids_require_all_sources_and_episode_metrics(self):
        from release.economic_evaluation.predictive_conformance import summary_errors

        self.assertTrue(summary_errors([{}] * 930))
        self.assertTrue(summary_errors([]))

    def test_malformed_lineage_is_reported_without_crashing(self):
        from release.economic_evaluation.predictive_conformance import validate

        row = {
            "world": 0,
            "replication": 0,
            "origin_month": 12,
            "target_month": 12,
            "horizon": 0,
            "fit_months": [9, 10, 11],
            "seed": 1000200,
            "input_lineage": {"case_ids": ["0/0/nope/0"], "demand_horizon": "month"},
        }
        result = validate({"decisions": [row]})
        self.assertFalse(result["passed"])

    def test_predict_reads_only_fit_months(self):
        from unittest.mock import patch

        from release.economic_evaluation.predictive import predict
        from zeroth.econ.probabilistic import ForecastReadiness

        class Poison:
            def __getitem__(self, key):
                raise AssertionError("future measurement was read")

        class Diagnostic:
            def model_dump(self, mode):
                return {"actions": [], "reason_codes": ["mechanical_test"]}

        history = [Poison() for _ in range(16)]
        for month in (9, 10, 11):
            history[month] = {
                "measurement": [((1, 100, True, False), (0.6, 100, True, False))] * 100,
                "demand": 1000,
            }
        context = {"origin_month": 12}
        errors = []
        with patch(
            "release.economic_evaluation.predictive.candidate.recommend_model_migration",
            return_value=Diagnostic(),
        ) as forecast:
            report, failure = predict(
                0, 0, history, [9, 10, 11], ForecastReadiness(), 1000200, False, errors, context
            )
        self.assertIsNone(failure, errors)
        self.assertIsNotNone(report)
        evidence = forecast.call_args.args[0]
        self.assertEqual(len(evidence.incumbent), 300)
        self.assertEqual(
            {int(row.case_id.split("/")[2]) for row in evidence.incumbent}, {9, 10, 11}
        )

    def test_summary_wiring_produces_all_930_identities(self):
        from release.economic_evaluation.predictive import forecast_summaries
        from release.economic_evaluation.predictive_conformance import summary_errors, validate

        rows = {"metrics": [], "controls": [], "decisions": []}
        summaries = [
            row
            for source in ("candidate", "true_law", "biased", "overconfident")
            for row in forecast_summaries(rows, source)
        ]
        self.assertEqual(len(summaries), 930)
        self.assertFalse(summary_errors(summaries))
        del summaries[0]["available"]
        self.assertTrue(summary_errors(summaries))
        # Empty raw data still fails; passing the summaries exercises CLI wiring.
        self.assertFalse(validate(rows, summaries)["passed"])
