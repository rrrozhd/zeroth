"""Regulus economics module -- cost tracking for every LLM call.

Public API:
- InstrumentedProviderAdapter: wraps any ProviderAdapter to emit cost events
- RegulusClient: thin wrapper around the Regulus SDK InstrumentationClient
- CostEstimator: USD cost estimation via litellm pricing data

Every export resolves lazily. Economics analytics read runs, audit records, and
provider adapters, so importing this package eagerly pulled the runtime and
governance layers into anything that touched it -- historically including
``zeroth.core.config.settings``, which only needed ``RegulusSettings`` (now
defined in :mod:`zeroth.platform.config.models` and republished from
:mod:`zeroth.core.econ.models`). That made the platform layer transitively
import the run domain. Resolving on first attribute access keeps
``from zeroth.core.econ import X`` working unchanged while letting a submodule
be imported on its own.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Re-exported via __getattr__ but deliberately absent from __all__, as before.
    from zeroth.core.econ.adapter import (
        InstrumentedProviderAdapter as InstrumentedProviderAdapter,
    )
    from zeroth.core.econ.budget import BudgetCheckResult, BudgetEnforcer
    from zeroth.core.econ.client import RegulusClient
    from zeroth.core.econ.cost import CostEstimator
    from zeroth.core.econ.opportunities import NodeSpend, SpendReport, spend_opportunities
    from zeroth.core.econ.quality import (
        QualityEconomicsReport,
        RunQualityVerdict,
        quality_economics,
        read_quality_verdict,
    )
    from zeroth.core.econ.rightsizing import ModelOption, RightsizingResult, describe, recommend
    from zeroth.core.econ.rightsizing_experiment import (
        CandidateOutcome,
        CorrectnessScorer,
        EquivalenceScorer,
        ExperimentReport,
        HarvestStats,
        build_experiment_dataset,
        build_labeled_dataset,
        run_experiment,
    )
    from zeroth.core.econ.unit_economics import (
        TenantEconomics,
        UnitEconomicsReport,
        WorkflowEconomics,
        unit_economics,
    )
    from zeroth.core.econ.waste import (
        EconReport,
        EconThresholdError,
        WasteFinding,
        WasteKind,
        WasteKindTotal,
        WasteRollup,
        WasteRollupFinding,
        analyze_run,
        waste_gate,
        waste_rollup,
    )

_EXPORTS = {
    "InstrumentedProviderAdapter": "adapter",
    "BudgetCheckResult": "budget",
    "BudgetEnforcer": "budget",
    "RegulusClient": "client",
    "CostEstimator": "cost",
    "NodeSpend": "opportunities",
    "SpendReport": "opportunities",
    "spend_opportunities": "opportunities",
    "QualityEconomicsReport": "quality",
    "RunQualityVerdict": "quality",
    "quality_economics": "quality",
    "read_quality_verdict": "quality",
    "ModelOption": "rightsizing",
    "RightsizingResult": "rightsizing",
    "describe": "rightsizing",
    "recommend": "rightsizing",
    "CandidateOutcome": "rightsizing_experiment",
    "CorrectnessScorer": "rightsizing_experiment",
    "EquivalenceScorer": "rightsizing_experiment",
    "ExperimentReport": "rightsizing_experiment",
    "HarvestStats": "rightsizing_experiment",
    "build_experiment_dataset": "rightsizing_experiment",
    "build_labeled_dataset": "rightsizing_experiment",
    "run_experiment": "rightsizing_experiment",
    "TenantEconomics": "unit_economics",
    "UnitEconomicsReport": "unit_economics",
    "WorkflowEconomics": "unit_economics",
    "unit_economics": "unit_economics",
    "EconReport": "waste",
    "EconThresholdError": "waste",
    "WasteFinding": "waste",
    "WasteKind": "waste",
    "WasteKindTotal": "waste",
    "WasteRollup": "waste",
    "WasteRollupFinding": "waste",
    "analyze_run": "waste",
    "waste_gate": "waste",
    "waste_rollup": "waste",
}

__all__ = [
    "BudgetCheckResult",
    "BudgetEnforcer",
    "CandidateOutcome",
    "CorrectnessScorer",
    "CostEstimator",
    "EconReport",
    "EconThresholdError",
    "EquivalenceScorer",
    "ExperimentReport",
    "HarvestStats",
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
]


def __getattr__(name: str) -> object:
    """Resolve a public econ symbol from its submodule on first access.

    The resolved value is cached in the package namespace. That is not just an
    optimization: ``unit_economics`` names both a submodule and the function it
    exports, and importing the submodule binds it as an attribute of this
    package. Caching the function over it reproduces what the previous eager
    ``from ... import unit_economics`` did, so ``zeroth.core.econ.unit_economics``
    keeps resolving to the callable.
    """
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))


# Names exported by this package that are also the name of one of its
# submodules. Importing a submodule binds it as an attribute of its package,
# which happens *after* the submodule finishes executing and therefore
# overwrites anything the lazy loader cached. Module-level ``__getattr__`` (PEP
# 562) cannot help, because it only runs when normal lookup fails and the
# shadowing submodule makes it succeed.
#
# The package used to resolve this by importing every submodule eagerly, so the
# ``from ... import unit_economics`` line in this file ran last and won. Exports
# are lazy now -- eagerly importing this submodule would pull the run domain
# into the platform layer -- so the collision is resolved explicitly instead.
_SHADOWED_BY_SUBMODULE = frozenset({"unit_economics"})


class _EconPackage(types.ModuleType):
    """Package type that keeps exported callables from being shadowed by submodules."""

    def __getattribute__(self, name: str) -> object:
        if name not in _SHADOWED_BY_SUBMODULE:
            return types.ModuleType.__getattribute__(self, name)
        cached = types.ModuleType.__getattribute__(self, "__dict__").get(name)
        if cached is not None and not isinstance(cached, types.ModuleType):
            return cached
        value = getattr(importlib.import_module(f"{__name__}.{_EXPORTS[name]}"), name)
        types.ModuleType.__getattribute__(self, "__dict__")[name] = value
        return value


sys.modules[__name__].__class__ = _EconPackage
