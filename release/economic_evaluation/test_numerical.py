import unittest


class NumericalHarnessTests(unittest.TestCase):
    def test_manifest_and_frequency_checker(self):
        from release.economic_evaluation.numerical import check_frequencies, manifest

        self.assertEqual(manifest()["total_atom_checks_M"], 400)
        self.assertEqual(manifest()["seeds"], list(range(100)))
        self.assertTrue(check_frequencies([400] * 5000 + [800] * 5000, "K03_savings")["passed"])
        self.assertFalse(check_frequencies([400] * 10000, "K03_savings")["passed"])
        self.assertFalse(check_frequencies([999] * 10000, "K03_savings")["passed"])
        self.assertFalse(check_frequencies([400, 800], "K03_savings")["passed"])
