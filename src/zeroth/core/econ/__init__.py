"""Regulus economics module -- cost tracking for every LLM call.

Public API:
- InstrumentedProviderAdapter: wraps any ProviderAdapter to emit cost events
- RegulusClient: thin wrapper around the Regulus SDK InstrumentationClient
- CostEstimator: USD cost estimation via litellm pricing data
"""

from zeroth.core.econ.budget import BudgetEnforcer
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

__all__ = [
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


def __getattr__(name: str) -> object:  # noqa: N807
    """Lazy import for InstrumentedProviderAdapter to avoid circular imports."""
    if name == "InstrumentedProviderAdapter":
        from zeroth.core.econ.adapter import InstrumentedProviderAdapter

        return InstrumentedProviderAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
