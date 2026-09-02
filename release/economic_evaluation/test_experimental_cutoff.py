"""Public recommendation guardrail; diagnostics do not establish reliability."""

import unittest

from release.economic_evaluation.test_invariants import evidence, policy
from zeroth.econ import probabilistic as candidate


class ExperimentalCutoffTests(unittest.TestCase):
    def test_public_calibrated_and_historical_override_cannot_authorize_migration(self):
        for require_calibrated in (True, False):
            result = candidate.recommend_model_migration(
                evidence(),
                policy=policy(require_calibrated_forecast=require_calibrated),
                simulations=100,
                seed=7,
            )
            self.assertEqual(result.verdict, "abstain")
            self.assertEqual(result.recommended_action, "collect_evidence")
            self.assertEqual(result.recommended_candidate_share, 0)
            self.assertEqual(result.recommended_routing, {})
            self.assertIn("experimental_predictive_reliability_unapproved", result.reason_codes)
            self.assertNotIn("risk_constraints_satisfied", result.reason_codes)
            self.assertEqual(result.actions[0].expected_monthly_savings_usd, 400)
            self.assertIs(result.evidence_lineage["future_request_variability_included"], False)

    def test_unapproved_reliability_not_solved_by_more_mc_draws(self):
        for count in (100, 1000):
            result = candidate.recommend_model_migration(
                evidence(), policy=policy(), simulations=count
            )
            self.assertEqual(result.recommended_action, "collect_evidence")
