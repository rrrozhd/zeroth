"""Deterministic red-first checks for approved nested future-request mechanics."""

import math
import random
import unittest
from unittest.mock import patch

from release.economic_evaluation.test_invariants import evidence, policy, qualified_diagnose
from zeroth.econ import probabilistic as candidate


class NestedPredictiveTests(unittest.TestCase):
    def test_work_overflow_abstains_before_any_random_draw(self):
        world = evidence(3)
        world.period_request_counts = [250_001]
        with patch.object(candidate.random, "Random", wraps=random.Random) as random_class:
            result = candidate.recommend_model_migration(world, policy=policy(), simulations=100)
        self.assertEqual(result.recommended_action, "collect_evidence")
        self.assertIn("simulation_work_budget_exceeded", result.reason_codes)
        self.assertEqual(result.actions, [])
        random_class.assert_not_called()

    def test_single_future_request_is_an_outcome_not_historical_average(self):
        world = evidence(3, heterogeneous=True)
        world.period_request_counts = [1]
        losses = []
        original = candidate.empirical_var_cvar

        def capture(values, *, confidence):
            losses.extend(values)
            return original(values, confidence=confidence)

        with patch.object(candidate, "empirical_var_cvar", capture):
            qualified_diagnose(world, policy(), simulations=100, seed=7)
        self.assertEqual(len(losses), 100)
        self.assertTrue(
            all(min(abs(value - atom) for atom in (-0.8, -0.7, -0.6)) < 1e-9 for value in losses)
        )

    def test_outer_sample_count_and_inner_future_count_are_distinct(self):
        base = random.Random

        class CountingRandom(base):
            draws = 0

            def randrange(self, *args, **kwargs):
                type(self).draws += 1
                return super().randrange(*args, **kwargs)

        world = evidence(501, heterogeneous=True)
        world.period_request_counts = [2]
        with patch.object(candidate.random, "Random", CountingRandom):
            qualified_diagnose(world, policy(), simulations=100)
        self.assertEqual(CountingRandom.draws, (501 + 2) * 100)

    def test_approved_familywise_radius_and_boundary_classification(self):
        interval = getattr(candidate, "_breach_probability_interval", None)
        self.assertIsNotNone(interval, "missing approved fixed-N numerical interval")
        result = interval(0.05, simulations=10_000, action_count=2, limit=0.05)
        radius = math.sqrt(math.log(2 * 4 * 2 / 0.01) / 20_000)
        self.assertAlmostEqual(result["lower"], 0.05 - radius)
        self.assertAlmostEqual(result["upper"], 0.05 + radius)
        self.assertEqual(result["status"], "indeterminate")
        self.assertEqual(
            interval(0, simulations=10_000, action_count=2, limit=0.05)["status"], "qualified"
        )
        self.assertEqual(
            interval(0.2, simulations=10_000, action_count=2, limit=0.05)["status"], "infeasible"
        )

    def test_point_feasible_is_not_sampled_qualified_at_small_n(self):
        world = evidence(3)
        world.period_request_counts = [2]
        result = qualified_diagnose(
            world, policy(max_constraint_breach_probability=0.05), simulations=100
        )
        self.assertTrue(result.actions[0].feasible)
        self.assertEqual(result.recommended_action, "collect_evidence")
        self.assertIn("mc_probability_indeterminate", result.reason_codes)

    def test_new_provenance_does_not_lift_public_cutoff(self):
        world = evidence(3)
        world.period_request_counts = [2]
        result = candidate.recommend_model_migration(world, policy=policy(), simulations=100)
        self.assertEqual(result.recommended_action, "collect_evidence")
        self.assertIs(result.evidence_lineage.get("future_request_variability_included"), False)
        self.assertEqual(result.evidence_lineage.get("predictive_reliability"), "unapproved")
        self.assertIn("risk_law_unqualified", result.reason_codes)
