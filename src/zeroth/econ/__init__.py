"""Regulus economics module -- cost tracking for every LLM call.

Public API:
- InstrumentedProviderAdapter: wraps any ProviderAdapter to emit cost events
- RegulusClient: thin wrapper around the Regulus SDK InstrumentationClient
- CostEstimator: USD cost estimation via litellm pricing data
"""

from zeroth.econ.budget import BudgetEnforcer
from zeroth.econ.client import RegulusClient
from zeroth.econ.cost import CostEstimator

__all__ = [
    "BudgetEnforcer",
    "RegulusClient",
    "CostEstimator",
]


def __getattr__(name: str):  # noqa: N807
    """Lazy import for InstrumentedProviderAdapter to avoid circular imports."""
    if name == "InstrumentedProviderAdapter":
        from zeroth.econ.adapter import InstrumentedProviderAdapter

        return InstrumentedProviderAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
