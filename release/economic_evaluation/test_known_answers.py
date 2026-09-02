"""Frozen answers are hand-transcribed from the contract, never candidate-generated."""

import json
import unittest
from fractions import Fraction
from pathlib import Path

FIXTURES = json.loads(Path(__file__).with_name("known_answers_v1.json").read_text())


class CandidateTailTests(unittest.TestCase):
    def test_k05_fractional_worst_probability_mass(self):
        from zeroth.econ.probabilistic import empirical_var_cvar

        fixture = next(row for row in FIXTURES["fixtures"] if row["id"] == "K05")
        actual = empirical_var_cvar(
            [float(Fraction(value)) for value in fixture["losses"]],
            confidence=float(Fraction(fixture["confidence"])),
        )
        expected = tuple(float(Fraction(fixture["expected"][key])) for key in ("var", "cvar"))
        for observed, target in zip(actual, expected, strict=True):
            self.assertAlmostEqual(observed, target, delta=1e-9)

    def test_candidate_tail_matches_rational_laws(self):
        from itertools import permutations

        from release.economic_evaluation.reference import FiniteLaw
        from zeroth.econ.probabilistic import _empirical_quantile, empirical_var_cvar

        for losses in ([1, 2, 3, 100], [-100, -3, -2, -1], [3, 3, 3, 3]):
            law = FiniteLaw([(value, 1) for value in losses])
            for order in permutations(losses):
                for alpha in ("1/100", "3/5", "3/4", "99/100"):
                    confidence = Fraction(alpha)
                    observed = empirical_var_cvar(list(order), confidence=float(confidence))
                    target = (law.quantile(confidence), law.cvar(confidence))
                    for actual, expected in zip(observed, target, strict=True):
                        self.assertAlmostEqual(actual, float(expected), delta=1e-9)
                for probability in (0, 0.25, 0.5, 0.75, 1):
                    self.assertEqual(
                        _empirical_quantile(list(order), probability),
                        law.quantile(Fraction(probability)),
                    )

    def test_tail_and_quantile_reject_invalid_input(self):
        from zeroth.econ.probabilistic import _empirical_quantile, _quantile, empirical_var_cvar

        for values in ([], [float("nan")], [float("inf")], [float("-inf")]):
            for function in (_empirical_quantile, _quantile):
                with (
                    self.subTest(values=values, function=function.__name__),
                    self.assertRaises(ValueError),
                ):
                    function(values, 0.5)
            with self.assertRaises(ValueError):
                empirical_var_cvar(values, confidence=0.6)
        for probability in (-0.1, 1.1, float("nan"), float("inf")):
            for function in (_empirical_quantile, _quantile):
                with (
                    self.subTest(probability=probability, function=function.__name__),
                    self.assertRaises(ValueError),
                ):
                    function([1, 2], probability)
            with self.assertRaises(ValueError):
                empirical_var_cvar([1, 2], confidence=probability)


class ReferenceTests(unittest.TestCase):
    def test_frozen_worlds(self):
        from release.economic_evaluation.reference import evaluate_fixture

        for fixture in FIXTURES["fixtures"]:
            with self.subTest(fixture=fixture["id"]):
                self.assertEqual(evaluate_fixture(fixture), fixture["expected"])

    def test_rational_properties_and_weighted_boundary(self):
        from itertools import permutations

        from release.economic_evaluation.reference import FiniteLaw, choose_action

        law = FiniteLaw([(1, 2), (3, 1), (100, 1)])
        self.assertEqual(law.quantile(Fraction(3, 4)), 3)
        self.assertEqual(law.cvar(Fraction(3, 5)), Fraction(509, 8))
        self.assertEqual(law.quantile(0), 1)
        self.assertEqual(law.quantile(1), 100)
        self.assertEqual(law.breach_probability(3), Fraction(1, 4))
        actions = [("hold", "100", "0"), ("A", "80", "1/100"), ("B", "60", "1/10")]
        for order in permutations(actions):
            self.assertEqual(choose_action(order, "1/20"), "A")
        self.assertEqual(choose_action(actions + [("dominated", "90", "1/50")], "1/20"), "A")
        self.assertEqual(choose_action(actions, "0"), "hold")
        for atoms in ([], [(1, -1)], [(1, 0)], [(float("nan"), 1)]):
            with self.assertRaises(ValueError):
                FiniteLaw(atoms)

    def test_oracle_has_no_candidate_imports(self):
        import ast

        source = Path(__file__).with_name("reference.py").read_text()
        tree = ast.parse(source)
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module)
        self.assertEqual(modules, ["__future__", "fractions"])
