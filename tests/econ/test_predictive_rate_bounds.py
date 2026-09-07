"""The rate fields say what they are: ``*_p05``/``*_p95`` are a conservative band and the
``*_lower_bound``/``*_upper_bound`` fields are the Wilson predictive envelope (report §2 A4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.econ._forecast_fixtures import diagnose, evidence, policy
from zeroth.econ import probabilistic as subject

DOC = Path(__file__).resolve().parents[2] / "docs/how-to/probabilistic-model-migration.md"


def test_bounds_are_the_predictive_envelope_and_the_percentile_fields_contain_it():
    world = evidence(count=400, periods=1)
    report = diagnose(world, policy(max_quality_drop=1.0, max_constraint_breach_probability=1.0), simulations=100)
    action = report.actions[0]
    incumbent, candidate = subject._paired_observations(world)
    success = subject._routed_rate_predictive_interval(
        incumbent, candidate, share=1.0, cohort_shares={}, attribute="accepted", future_trials=1_000
    )
    critical = subject._routed_rate_predictive_interval(
        incumbent, candidate, share=1.0, cohort_shares={}, attribute="critical_error", future_trials=1_000
    )

    assert (action.success_rate_lower_bound, action.success_rate_upper_bound) == success
    assert (action.critical_error_rate_lower_bound, action.critical_error_rate_upper_bound) == critical
    # Zero observed critical errors: the simulated 95th percentile is 0, so the
    # legacy p95 field carries the finite-sample envelope, not a percentile.
    assert action.critical_error_rate_upper_bound == pytest.approx(0.017680380334649776)
    assert action.critical_error_rate_p95 == action.critical_error_rate_upper_bound
    assert action.critical_error_rate_p05 == action.critical_error_rate_lower_bound == 0.0
    assert action.success_rate_p05 <= action.success_rate_lower_bound
    assert action.success_rate_p95 >= action.success_rate_upper_bound
    assert 0.0 <= action.success_rate_lower_bound <= action.success_rate_upper_bound <= 1.0


def test_documentation_names_the_bounds_and_describes_the_percentile_fields():
    text = DOC.read_text(encoding="utf-8")

    assert "success_rate_lower_bound" in text
    assert "critical_error_rate_upper_bound" in text
    assert "conservative" in text
