"""Legacy import path for :mod:`zeroth.econ.instrumentation.langgraph`."""

from zeroth.econ.instrumentation.langgraph import (
    LangGraphTelemetryAdapter,
    instrument_langgraph_graph,
)

__all__ = [
    "LangGraphTelemetryAdapter",
    "instrument_langgraph_graph",
]
