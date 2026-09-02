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
        self.assertEqual(actual, expected)
