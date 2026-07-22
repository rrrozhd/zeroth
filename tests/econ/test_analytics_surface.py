"""Canonical import surface for the economics analytics and instrumentation packages.

Non-golden boundary tests for the Task 14 econ split: the canonical
``zeroth.econ.analytics`` and ``zeroth.econ.instrumentation`` packages must
publish the same objects the legacy ``zeroth.core.econ`` path keeps
republishing (lazily), and the packages must stay cold-importable from a
fresh interpreter in either order.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


PACKAGE_EXPORTS = (
    "BudgetEnforcer",
    "CandidateOutcome",
    "CorrectnessScorer",
    "CostEstimator",
    "EconReport",
    "EconThresholdError",
    "EquivalenceScorer",
    "ExperimentReport",
    "HarvestStats",
    "InstrumentedProviderAdapter",
    "ModelOption",
    "NodeSpend",
    "QualityEconomicsReport",
    "RegulusClient",
    "RightsizingResult",
    "RunQualityVerdict",
    "SpendReport",
    "TenantEconomics",
    "UnitEconomicsReport",
    "WasteFinding",
    "WasteKind",
    "WasteKindTotal",
    "WasteRollup",
    "WasteRollupFinding",
    "WorkflowEconomics",
    "analyze_run",
    "build_experiment_dataset",
    "build_labeled_dataset",
    "describe",
    "quality_economics",
    "read_quality_verdict",
    "recommend",
    "run_experiment",
    "spend_opportunities",
    "unit_economics",
    "waste_gate",
    "waste_rollup",
)

ANALYTICS_MODULES = (
    "adapter",
    "budget",
    "client",
    "cost",
    "models",
    "opportunities",
    "quality",
    "rightsizing",
    "rightsizing_experiment",
    "service_auth",
    "unit_economics",
    "waste",
)

INSTRUMENTATION_EXPORTS = (
    "AutoInstrumentationConfig",
    "CostProfileInput",
    "ExecutionCostBreakdown",
    "ExecutionEvent",
    "InstrumentationClient",
    "InstrumentationConfig",
    "LibraryContext",
    "OutcomeEvent",
    "build_cost_profile_input",
    "configure",
    "disable_auto_instrumentation",
    "enable_auto_instrumentation",
    "instrument_anthropic_async_client",
    "instrument_anthropic_client",
    "instrument_langchain_app",
    "instrument_langchain_async_runnable",
    "instrument_langchain_callback_handler",
    "instrument_langchain_runnable",
    "instrument_langgraph_graph",
    "instrument_openai_async_client",
    "instrument_openai_client",
    "join_key_context",
    "track_execution",
    "track_outcome",
    "with_instrumentation",
)


def test_econ_package_exports_are_the_same_through_both_paths() -> None:
    import zeroth.core.econ as legacy
    import zeroth.econ.analytics as canonical

    for name in PACKAGE_EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


def test_unit_economics_stays_the_callable_not_the_submodule() -> None:
    import types

    import zeroth.core.econ as legacy
    import zeroth.econ.analytics as canonical

    assert not isinstance(legacy.unit_economics, types.ModuleType)
    assert not isinstance(canonical.unit_economics, types.ModuleType)
    assert canonical.unit_economics is legacy.unit_economics


@pytest.mark.parametrize("module_name", ANALYTICS_MODULES)
def test_econ_analytics_modules_are_the_same_surface_through_both_paths(
    module_name: str,
) -> None:
    legacy_module = importlib.import_module(f"zeroth.core.econ.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.econ.analytics.{module_name}")

    for name in getattr(legacy_module, "__all__", []):
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


def test_instrumentation_sdk_is_the_same_surface_through_both_paths() -> None:
    import zeroth.core.econ.instrumentation as legacy
    import zeroth.econ.instrumentation as canonical

    for name in INSTRUMENTATION_EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


def test_regulus_settings_stays_the_platform_owned_model() -> None:
    from zeroth.econ.analytics.models import RegulusSettings as RepublishedSettings
    from zeroth.platform.config.models import RegulusSettings as PlatformSettings

    assert RepublishedSettings is PlatformSettings


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.econ.analytics", "zeroth.core.econ"),
        ("zeroth.core.econ", "zeroth.econ.analytics"),
        ("zeroth.econ.instrumentation", "zeroth.core.econ.instrumentation"),
        ("zeroth.core.econ.instrumentation", "zeroth.econ.instrumentation"),
    ],
)
def test_econ_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"


def test_importing_econ_models_keeps_the_run_domain_off_the_import_path() -> None:
    """The lazy legacy init and the analytics package must not eagerly load runs."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import zeroth.econ.analytics.models\n"
            "assert 'zeroth.core.runs' not in sys.modules, 'run domain loaded'\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
