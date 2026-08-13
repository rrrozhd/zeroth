"""Measurement provenance shared across runtime and economic boundaries."""

from enum import StrEnum


class MeasurementState(StrEnum):
    """Whether a value was observed, derived, or unavailable."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNMEASURED = "unmeasured"
