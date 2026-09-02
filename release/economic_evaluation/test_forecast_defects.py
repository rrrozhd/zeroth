"""Forecast correctness regressions beyond the initial narrow tail tests."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from release.economic_evaluation.test_invariants import evidence, policy
from zeroth.econ import probabilistic as candidate


class ForecastDefectTests(unittest.TestCase):
    def test_skewed_quantile_interval_need_not_contain_the_mean(self):
        row = candidate.ForecastCalibrationObservation(
            forecast_id="skew",
            metric="cost",
            predicted_mean=4,
            predicted_low=0,
            predicted_high=0,
            observed=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(row.predicted_mean, 4)

    def run_world(self, world):
        return candidate._diagnose_model_migration(world, policy=policy(), simulations=100, seed=7)

    def test_missing_candidate_units_abstains_instead_of_complete_case_forecast(self):
        world = evidence(6)
        world.candidate.pop()
        result = self.run_world(world)
        self.assertEqual(result.recommended_action, "collect_evidence")
        self.assertEqual(result.actions, [])

    def test_missing_critical_measurement_is_not_measured_false(self):
        payload = evidence().model_dump()
        del payload["candidate"][0]["critical_error"]
        result = self.run_world(candidate.MigrationEvidence.model_validate(payload))
        self.assertEqual(result.recommended_action, "collect_evidence")
        self.assertEqual(result.actions, [])

    def test_positive_savings_below_presentation_rounding_still_count(self):
        world = evidence()
        for row in world.candidate:
            row.cost_usd = Decimal("0.9999999999")
        result = self.run_world(world)
        self.assertEqual(result.recommended_action, "ship_candidate")
        self.assertAlmostEqual(
            float(result.actions[0].expected_monthly_savings_usd), 1e-7, delta=1e-9
        )

    def test_nan_calibration_mean_rejected_not_labeled_calibrated(self):
        with self.assertRaises(ValueError):
            candidate.ForecastCalibrationObservation(
                forecast_id="f",
                metric="cost",
                predicted_mean=float("nan"),
                predicted_low=0,
                predicted_high=2,
                observed=1,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_nonfinite_routing_and_policy_shares_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate.CohortRoutingAction(
                    action_id="bad", cohort_candidate_shares={"left": value}
                )
            with self.subTest(policy=value), self.assertRaises(ValueError):
                policy(candidate_shares=[value])

    def test_duplicate_calibration_forecast_does_not_create_periods(self):
        row = candidate.ForecastCalibrationObservation(
            forecast_id="one-forecast",
            metric="cost",
            predicted_mean=1,
            predicted_low=0.9,
            predicted_high=1.1,
            observed=1,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = candidate.assess_forecast_readiness([row] * 6, required_metrics={"cost"})
        self.assertNotEqual(result.calibration_state, "calibrated")
        self.assertEqual(result.calibration_periods, 1)

    def test_identical_paired_heterogeneous_outcomes_have_zero_incremental_loss(self):
        world = evidence(12, heterogeneous=True)
        world.incumbent = [row.model_copy() for row in world.candidate]
        result = self.run_world(world)
        self.assertEqual(result.recommended_action, "keep_incumbent")
        self.assertEqual(result.actions[0].cvar_loss_usd, 0)
        self.assertEqual(result.actions[0].monthly_savings_p05_usd, 0)
        self.assertEqual(result.actions[0].monthly_savings_p95_usd, 0)
