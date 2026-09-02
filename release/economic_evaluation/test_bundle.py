import unittest


class BundleTests(unittest.TestCase):
    def test_candidate_identity_resolves_this_repository(self):
        from release.economic_evaluation.cli import ROOT, identity

        self.assertTrue((ROOT / "src/zeroth/econ/probabilistic.py").is_file())
        self.assertEqual(identity()["worktree"], str(ROOT))

    def test_every_required_nonpass_blocks_acceptance(self):
        from release.economic_evaluation.cli import acceptance_passed

        self.assertFalse(acceptance_passed([]))
        self.assertTrue(acceptance_passed([{"required": True, "status": "passed"}]))
        for status in ("failed", "skipped", "blocked", "untested", "not_implemented"):
            self.assertFalse(acceptance_passed([{"required": True, "status": status}]))
        self.assertTrue(
            acceptance_passed(
                [{"required": True, "status": "passed"}, {"required": False, "status": "blocked"}]
            )
        )
