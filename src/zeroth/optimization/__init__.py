"""Economic optimization for production AI workflows.

This is Zeroth's primary product surface. It organizes the existing economics
engine around one operational flow without duplicating or relocating it:

1. measure cost per accepted outcome;
2. find waste and optimization opportunities;
3. backtest a cheaper candidate against recorded work; and
4. enforce an economic release gate.

The underlying :mod:`zeroth.econ.analytics` package remains public and
backward-compatible. This module gives new integrations a small, intentional
entry point while the broader runtime stays available as supporting
infrastructure.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.econ.analytics.opportunities import (
        spend_opportunities as find_optimization_opportunities,
    )
    from zeroth.econ.analytics.rightsizing import recommend as recommend_model_change
    from zeroth.econ.analytics.rightsizing_experiment import (
        build_experiment_dataset as build_backtest_dataset,
    )
    from zeroth.econ.analytics.rightsizing_experiment import (
        run_experiment as backtest_model_change,
    )
    from zeroth.econ.analytics.unit_economics import (
        unit_economics as measure_unit_economics,
    )
    from zeroth.econ.analytics.waste import (
        analyze_run as analyze_economic_waste,
    )
    from zeroth.econ.analytics.waste import (
        waste_gate as enforce_economic_gate,
    )
    from zeroth.econ.decisioning import compare_workflow_versions

_EXPORTS = {
    "measure_unit_economics": ("zeroth.econ.analytics.unit_economics", "unit_economics"),
    "analyze_economic_waste": ("zeroth.econ.analytics.waste", "analyze_run"),
    "find_optimization_opportunities": (
        "zeroth.econ.analytics.opportunities",
        "spend_opportunities",
    ),
    "recommend_model_change": ("zeroth.econ.analytics.rightsizing", "recommend"),
    "build_backtest_dataset": (
        "zeroth.econ.analytics.rightsizing_experiment",
        "build_experiment_dataset",
    ),
    "backtest_model_change": (
        "zeroth.econ.analytics.rightsizing_experiment",
        "run_experiment",
    ),
    "compare_workflow_versions": ("zeroth.econ.decisioning", "compare_workflow_versions"),
    "enforce_economic_gate": ("zeroth.econ.analytics.waste", "waste_gate"),
}

__all__ = [
    "analyze_economic_waste",
    "backtest_model_change",
    "build_backtest_dataset",
    "compare_workflow_versions",
    "enforce_economic_gate",
    "find_optimization_opportunities",
    "measure_unit_economics",
    "recommend_model_change",
]


def __getattr__(name: str) -> object:
    """Resolve one product operation from the existing economics engine."""
    target = _EXPORTS.get(name)
    if target is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module_name, symbol_name = target
    value = getattr(importlib.import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
