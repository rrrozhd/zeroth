"""Replay specified scenario laws through production arithmetic and point decisions.

Scripted sampling is an adapter for exhaustive finite fixtures, NOT Monte Carlo
evidence, a predictive experiment or public authorization. Goldens are frozen.
The real sampler has separate preregistered checks. Public cutoff tests are separate.
"""

import unittest
from decimal import Decimal
from fractions import Fraction
from unittest.mock import patch

from release.economic_evaluation.test_invariants import evidence, policy
from release.economic_evaluation.test_known_answers import FIXTURES
from zeroth.econ import probabilistic as candidate


def replay(world, rules, *, breach_scenarios=None):
    class EnumeratedScenarios:
        def __init__(self, seed):
            self.scenario = -1
            self.position = 0
            self.future = False
            self.demand = 0

        def choice(self, values):
            self.scenario += 1
            self.position = 0
            self.future = True
            self.demand = values[self.scenario % len(values)]
            return self.demand

        def randrange(self, size):
            position = self.position
            self.position += 1
            if not self.future or breach_scenarios is None:
                return position % size
            # Input rows are sorted left/right, then safe/high latency by design.
            half = size // 2
            side = int(position % size >= half)
            return side * half + (half - 1 if self.scenario < breach_scenarios[side] else 0)

        def random(self):
            if self.position == self.demand:
                self.future = False
                self.position = 0
            return 0.5

    with patch.object(candidate.random, "Random", EnumeratedScenarios):
        return candidate._diagnose_model_migration(world, policy=rules, simulations=100, seed=0)


def risk_world(left_price="0.6", right_price="0.6"):
    world = evidence(100)
    world.period_request_counts = [100]
    # Fifty measurements per cohort, with safe and high-latency representatives.
    for rows in (world.incumbent, world.candidate):
        for index, row in enumerate(rows):
            row.cohort = "left" if index < 50 else "right"
    for index, row in enumerate(world.candidate):
        row.cost_usd = Decimal(left_price if index < 50 else right_price)
        row.latency_ms = 200 if index in (49, 99) else 100
    rules = policy(
        routing_actions=[
            candidate.CohortRoutingAction(
                action_id="A", cohort_candidate_shares={"left": 1, "right": 0}
            ),
            candidate.CohortRoutingAction(
                action_id="B", cohort_candidate_shares={"left": 1, "right": 1}
            ),
        ],
        max_p95_latency_ms=150,
        max_constraint_breach_probability=0.05,
    )
    return world, rules


class FrozenCandidateAdapterTests(unittest.TestCase):
    def expected(self, fixture_id):
        return next(row["expected"] for row in FIXTURES["fixtures"] if row["id"] == fixture_id)

    def test_k01_identical_costs_and_loss(self):
        world = evidence()
        world.candidate = [row.model_copy() for row in world.incumbent]
        result = replay(world, policy())
        self.assertEqual(result.recommended_action, "keep_incumbent")
        self.assertEqual(
            result.actions[0].expected_monthly_savings_usd,
            Fraction(self.expected("K01")["savings"]),
        )
        self.assertEqual(result.actions[0].cvar_loss_usd, Fraction(self.expected("K01")["loss"]))

    def test_k02_constant_monthly_prices(self):
        result = replay(evidence(), policy())
        target = self.expected("K02")
        self.assertEqual(result.actions[0].expected_monthly_cost_usd, Fraction(target["cost"]))
        self.assertEqual(
            result.actions[0].expected_monthly_savings_usd, Fraction(target["savings"])
        )
        self.assertEqual(result.actions[0].cvar_loss_usd, Fraction(target["loss"]))

    def test_k03_exact_two_state_demand(self):
        world = evidence()
        world.period_request_counts = [1000, 2000]
        result = replay(world, policy())
        action = result.actions[0]
        target = self.expected("K03")
        self.assertEqual(action.expected_monthly_savings_usd, Fraction(target["savings"]))
        self.assertEqual(action.monthly_savings_p05_usd, Fraction(target["savings_law"][0][0]))
        self.assertEqual(action.monthly_savings_p95_usd, Fraction(target["savings_law"][1][0]))

    def test_k04_k05_fractional_tail(self):
        for fixture_id in ("K04", "K05"):
            fixture = next(row for row in FIXTURES["fixtures"] if row["id"] == fixture_id)
            actual = candidate.empirical_var_cvar(
                [float(Fraction(value)) for value in fixture["losses"]],
                confidence=float(Fraction(fixture["confidence"])),
            )
            for value, key in zip(actual, ("var", "cvar"), strict=True):
                self.assertAlmostEqual(value, float(Fraction(fixture["expected"][key])), delta=1e-9)

    def test_k06_exact_four_breaches(self):
        world, rules = risk_world()
        result = replay(world, rules, breach_scenarios=(0, 4))
        self.assertEqual(
            result.actions[1].probability_latency_breach,
            float(Fraction(self.expected("K06")["probability"])),
        )

    def test_k07_never_select_infeasible_cheaper_action(self):
        world, rules = risk_world()
        result = replay(world, rules, breach_scenarios=(1, 10))
        self.assertEqual([row.expected_monthly_cost_usd for row in result.actions], [80, 60])
        self.assertEqual([row.probability_latency_breach for row in result.actions], [0.01, 0.10])
        self.assertEqual([row.feasible for row in result.actions], [True, False])
        selected = candidate._point_winner(result.actions)
        self.assertEqual(selected.action_id, self.expected("K07")["choice"])
        self.assertEqual(result.recommended_action, "collect_evidence")

    def test_k08_hold_when_only_cheap_action_is_infeasible(self):
        world, rules = risk_world("1.2", "0")
        result = replay(world, rules, breach_scenarios=(0, 10))
        for row, target in zip(result.actions, (110, 60), strict=True):
            self.assertAlmostEqual(float(row.expected_monthly_cost_usd), target, delta=1e-9)
        self.assertEqual(result.recommended_action, "keep_incumbent")

    def test_k09_missing_evidence_and_calibration(self):
        for kind in ("calibration", "candidate"):
            world = evidence()
            if kind == "calibration":
                world.readiness = candidate.ForecastReadiness()
            else:
                world.candidate.pop()
            result = replay(world, policy())
            self.assertEqual(result.recommended_action, self.expected("K09")["choice"])
            self.assertEqual(result.actions, [])

    def test_k10_signed_penalized_loss_keeps_savings_separate(self):
        world = evidence(500)
        world.candidate[-1].critical_error = True
        result = replay(world, policy(critical_error_penalty_usd=Decimal("300")))
        self.assertAlmostEqual(
            float(result.actions[0].cvar_loss_usd),
            float(Fraction(self.expected("K10")["loss"])),
            delta=1e-9,
        )
        self.assertEqual(
            result.actions[0].expected_monthly_savings_usd,
            Fraction(self.expected("K10")["savings"]),
        )

    def test_k11_point_equality_is_feasible_not_mc_certification(self):
        world, rules = risk_world()
        result = replay(world, rules, breach_scenarios=(0, 5))
        self.assertEqual(
            result.actions[1].probability_latency_breach,
            float(Fraction(self.expected("K11")["probability"])),
        )
        self.assertIs(result.actions[1].feasible, self.expected("K11")["feasible"])
        bounds = result.evidence_lineage["numerical_qualification"]["actions"]["B"]
        self.assertEqual(bounds["latency"]["status"], "indeterminate")

    def test_penalty_monotonicity_respects_incremental_error_sign(self):
        for increasing_errors, expected in (
            (True, [-400, 200, 800]),
            (False, [-400, -1000, -1600]),
        ):
            world = evidence(500)
            rows = world.candidate if increasing_errors else world.incumbent
            rows[-1].critical_error = True
            for penalty, target in zip((0, 300, 600), expected, strict=True):
                result = replay(world, policy(critical_error_penalty_usd=Decimal(penalty)))
                self.assertAlmostEqual(float(result.actions[0].cvar_loss_usd), target, delta=1e-9)
