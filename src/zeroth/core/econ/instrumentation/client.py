"""Legacy import path for :mod:`zeroth.econ.instrumentation.client`."""

from zeroth.econ.instrumentation.client import (
    InstrumentationClient,
    build_cost_profile_input,
    configure,
    disable_auto_instrumentation,
    enable_auto_instrumentation,
    join_key_context,
    track_execution,
    track_outcome,
    with_instrumentation,
)

__all__ = [
    "InstrumentationClient",
    "build_cost_profile_input",
    "configure",
    "disable_auto_instrumentation",
    "enable_auto_instrumentation",
    "join_key_context",
    "track_execution",
    "track_outcome",
    "with_instrumentation",
]
