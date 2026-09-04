"""Required aggregate-world witness; not a broad predictive experiment."""

import unittest
from unittest.mock import patch

from release.economic_evaluation.test_invariants import evidence, policy, qualified_diagnose
from zeroth.econ import probabilistic as candidate


class FutureRequestTests(unittest.TestCase):
    def test_two_future_requests_never_have_fractional_routing_counts(self):
        world = evidence(3)
        world.period_request_counts = [2]
        losses = []
        original = candidate.empirical_var_cvar

        def capture(values, *, confidence):
            losses.extend(values)
            return original(values, confidence=confidence)

        with patch.object(candidate, "empirical_var_cvar", capture):
            qualified_diagnose(
                world, policy(candidate_shares=[0.5]), simulations=100, seed=7
            )
        self.assertEqual(len(losses), 100)
        self.assertEqual(
            [loss for loss in losses if min(abs(loss - atom) for atom in (-0.8, -0.4, 0)) > 1e-9],
            [],
        )
