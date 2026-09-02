"""Private mathematical diagnostics; public authorization is tested separately."""

import random
import unittest
from decimal import Decimal
from unittest.mock import patch

from zeroth.econ import probabilistic as candidate


def evidence(count=6, heterogeneous=False):
    rows = [
        dict(
            case_id=f"unit-{index:04d}",
            cohort="left" if index % 2 else "right",
            cost_usd="1",
            latency_ms=100,
            accepted=True,
            critical_error=False,
            source="specified-finite-world",
        )
        for index in range(count)
    ]
    alternatives = [
        dict(
            row, cost_usd=str(Decimal("0.2") + Decimal(index % 3) / 10) if heterogeneous else "0.6"
        )
        for index, row in enumerate(rows)
    ]
    return candidate.MigrationEvidence(
        workload="frozen-monthly-fixture",
        incumbent_model="incumbent",
        candidate_model="candidate",
        incumbent=rows,
        candidate=alternatives,
        period_request_counts=[1000],
        demand_horizon="month",
        readiness=candidate.ForecastReadiness(calibration_state="calibrated", drift_state="stable"),
    )


def policy(**changes):
    values = dict(
        min_paired_cases=1,
        candidate_shares=[1.0],
        max_quality_drop=1,
        max_p95_latency_ms=1000,
        max_critical_error_rate=1,
        max_constraint_breach_probability=1,
        max_cvar_loss_usd=Decimal("100000"),
    )
    values.update(changes)
    return candidate.MigrationRiskPolicy(**values)


class CandidateInvariantTests(unittest.TestCase):
    def run_world(self, world, rules=None, seed=7):
        return candidate._diagnose_model_migration(
            world, policy=rules or policy(), simulations=100, seed=seed
        )

    def test_k02_and_model_swap(self):
        world = evidence()
        decision = self.run_world(world)
        action = decision.actions[0]
        self.assertEqual(decision.incumbent_model, "incumbent")
        self.assertEqual(decision.candidate_model, "candidate")
        self.assertEqual(decision.recommended_action, "ship_candidate")
        self.assertEqual(action.expected_monthly_cost_usd, Decimal("600"))
        self.assertEqual(action.expected_monthly_savings_usd, Decimal("400"))
        self.assertEqual(action.cvar_loss_usd, Decimal("-400"))
        swapped = world.model_copy(
            update=dict(
                incumbent=world.candidate,
                candidate=world.incumbent,
                incumbent_model="candidate",
                candidate_model="incumbent",
            )
        )
        reverse = self.run_world(swapped)
        self.assertEqual(reverse.recommended_action, "keep_incumbent")
        self.assertEqual(reverse.actions[0].expected_monthly_savings_usd, Decimal("-400"))
        self.assertEqual(reverse.actions[0].cvar_loss_usd, Decimal("400"))

    def test_identical_outcomes_hold(self):
        world = evidence()
        world.candidate = [row.model_copy() for row in world.incumbent]
        decision = self.run_world(world)
        self.assertEqual(decision.recommended_action, "keep_incumbent")
        self.assertEqual(decision.actions[0].expected_monthly_savings_usd, 0)
        self.assertEqual(decision.actions[0].cvar_loss_usd, 0)

    def test_missing_calibration_abstains_without_forecasts(self):
        world = evidence()
        world.readiness = candidate.ForecastReadiness()
        decision = self.run_world(world)
        self.assertEqual(decision.recommended_action, "collect_evidence")
        self.assertEqual(decision.actions, [])

    def test_duplicate_units_and_broken_cohorts_rejected(self):
        payload = evidence().model_dump()
        payload["candidate"].append(payload["candidate"][0])
        with self.assertRaises(ValueError):
            candidate.MigrationEvidence.model_validate(payload)
        payload = evidence().model_dump()
        payload["candidate"][0]["cohort"] = "different"
        with self.assertRaises(ValueError):
            candidate.MigrationEvidence.model_validate(payload)

    def test_reordering_and_renaming_units_preserves_forecasts(self):
        world = evidence(12, heterogeneous=True)
        expected = self.run_world(world)
        world.incumbent.reverse()
        world.candidate.reverse()
        self.assertEqual(self.run_world(world), expected)
        for rows in (world.incumbent, world.candidate):
            for row in rows:
                row.case_id = f"renamed-{99 - int(row.case_id.split('-')[1]):04d}"
        self.assertEqual(self.run_world(world), expected)

    def test_action_order_and_dominated_addition_preserve_forecasts(self):
        world = evidence(12, heterogeneous=True)
        actions = [
            candidate.CohortRoutingAction(action_id=name, cohort_candidate_shares=shares)
            for name, shares in [
                ("A", {"left": 0.7, "right": 0.2}),
                ("B", {"left": 0.1, "right": 0.8}),
            ]
        ]
        first = self.run_world(world, policy(routing_actions=actions))
        second = self.run_world(world, policy(routing_actions=list(reversed(actions))))
        self.assertEqual(
            {row.action_id: row for row in first.actions},
            {row.action_id: row for row in second.actions},
        )
        self.assertEqual(first.recommended_routing, second.recommended_routing)
        extra = candidate.CohortRoutingAction(
            action_id="dominated", cohort_candidate_shares={"left": 0.01}
        )
        third = self.run_world(world, policy(routing_actions=[extra] + actions))
        self.assertEqual(first.actions, third.actions[1:])

    def test_bootstrap_preserves_number_of_independent_units(self):
        base_random = random.Random

        class CountingRandom(base_random):
            draws = 0

            def randrange(self, *args, **kwargs):
                type(self).draws += 1
                return super().randrange(*args, **kwargs)

        with patch.object(candidate.random, "Random", CountingRandom):
            self.run_world(evidence(501))
        self.assertEqual(CountingRandom.draws, 501 * 100)

    def test_tighter_policy_cannot_expand_feasible_set(self):
        world = evidence(12, heterogeneous=True)
        broad = self.run_world(world, policy(candidate_shares=[0.25, 0.5, 1]))
        tight = self.run_world(
            world, policy(candidate_shares=[0.25, 0.5, 1], max_cvar_loss_usd=Decimal("-500"))
        )
        self.assertLessEqual(
            {row.action_id for row in tight.actions if row.feasible},
            {row.action_id for row in broad.actions if row.feasible},
        )
