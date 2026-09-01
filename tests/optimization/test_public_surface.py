"""The narrowed economic-debugger product surface.

These tests intentionally exercise aliases rather than duplicate behavior. The
existing economics implementation remains canonical; ``zeroth.optimization``
gives users one product-shaped entry point without breaking older imports.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]


def test_public_surface_follows_the_product_flow() -> None:
    import zeroth.optimization as optimization

    assert optimization.__all__ == [
        "analyze_economic_waste",
        "backtest_model_change",
        "build_backtest_dataset",
        "compare_workflow_versions",
        "enforce_economic_gate",
        "find_optimization_opportunities",
        "measure_unit_economics",
        "recommend_model_change",
    ]


def test_public_names_preserve_the_existing_implementations() -> None:
    import zeroth.optimization as optimization
    from zeroth.econ.analytics import (
        analyze_run,
        build_experiment_dataset,
        recommend,
        run_experiment,
        spend_opportunities,
        unit_economics,
        waste_gate,
    )
    from zeroth.econ.decisioning import compare_workflow_versions

    assert optimization.measure_unit_economics is unit_economics
    assert optimization.analyze_economic_waste is analyze_run
    assert optimization.find_optimization_opportunities is spend_opportunities
    assert optimization.recommend_model_change is recommend
    assert optimization.build_backtest_dataset is build_experiment_dataset
    assert optimization.backtest_model_change is run_experiment
    assert optimization.enforce_economic_gate is waste_gate
    assert optimization.compare_workflow_versions is compare_workflow_versions


def test_product_surface_is_cold_importable_without_loading_the_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import zeroth.optimization\n"
            "assert 'zeroth.runtime' not in sys.modules, 'runtime loaded eagerly'\n",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_distribution_metadata_leads_with_the_economic_debugger() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]

    assert project["description"] == (
        "Economic debugger and governed change evidence for production AI workflows"
    )
    assert {"economics", "optimization", "backtesting", "governance"} <= set(
        project["keywords"]
    )
