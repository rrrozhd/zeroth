"""Approved explicit horizon and durable missing-versus-false semantics."""

import importlib.util
import sys
import unittest
from pathlib import Path

from release.economic_evaluation.test_invariants import evidence, policy
from zeroth.econ import probabilistic as candidate


def sdk_models():
    path = Path(__file__).resolve().parents[2] / "packaging/sdk/src/zeroth/protocol/models.py"
    spec = importlib.util.spec_from_file_location("evaluation_sdk_models", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HorizonAndMissingnessTests(unittest.TestCase):
    def test_telemetry_missing_critical_flag_is_unknown(self):
        from types import SimpleNamespace

        from zeroth.econ.plane.decisioning.evidence import _critical

        self.assertIsNone(_critical(SimpleNamespace(outcome_payload_json={})))
        self.assertIsNone(
            _critical(SimpleNamespace(outcome_payload_json={"critical_error": "false"}))
        )
        self.assertIs(
            _critical(SimpleNamespace(outcome_payload_json={"critical_error": False})), False
        )

    def test_unknown_and_legacy_horizon_abstain(self):
        for explicit in (False, True):
            payload = evidence().model_dump()
            payload.pop("demand_horizon", None)
            if explicit:
                payload["demand_horizon"] = "unknown"
            world = candidate.MigrationEvidence.model_validate(payload)
            result = candidate.recommend_model_migration(world, policy=policy(), simulations=100)
            self.assertEqual(result.recommended_action, "collect_evidence")
            self.assertEqual(result.actions, [])

    def test_month_horizon_survives_domain_and_sdk_roundtrip(self):
        payload = evidence().model_dump()
        payload["demand_horizon"] = "month"
        for model in (candidate.MigrationEvidence, sdk_models().MigrationEvidence):
            world = model.model_validate(payload)
            self.assertEqual(
                model.model_validate_json(world.model_dump_json()).demand_horizon, "month"
            )
            self.assertEqual(
                world.model_json_schema()["properties"]["demand_horizon"]["enum"],
                ["month", "unknown"],
            )

    def test_missing_critical_measurement_survives_json_and_sdk(self):
        payload = evidence().model_dump()
        del payload["candidate"][0]["critical_error"]
        # The SDK and server both serialize and parse; none may invent false.
        for model in (sdk_models().MigrationEvidence, candidate.MigrationEvidence):
            world = model.model_validate(payload)
            payload = model.model_validate_json(world.model_dump_json()).model_dump(mode="json")
            self.assertNotIn("critical_error", payload["candidate"][0])
            self.assertIs(payload["candidate"][1]["critical_error"], False)

    def test_sdk_calibration_validation_matches_server(self):
        payload = dict(
            forecast_id="skew",
            metric="cost",
            predicted_mean=4,
            predicted_low=0,
            predicted_high=0,
            observed=0,
            observed_at="2026-01-01T00:00:00Z",
        )
        model = sdk_models().ForecastCalibrationObservation
        self.assertEqual(model.model_validate(payload).predicted_mean, 4)
        for field in ("predicted_mean", "predicted_low", "predicted_high", "observed"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                model.model_validate(dict(payload, **{field: float("nan")}))
