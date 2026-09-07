"""A producer's business-maturity declaration, separate from observation provenance."""

from datetime import datetime
from typing import Literal

OutcomeMaturity = Literal["unknown", "provisional", "final", "withdrawn"]


def validate_outcome_maturity(
    maturity: OutcomeMaturity,
    value: object,
    timestamp: datetime | None,
) -> None:
    if maturity == "unknown":
        return
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("declared outcome maturity requires an aware assertion timestamp")
    if maturity == "final" and value is None:
        raise ValueError("final outcomes require an observation")
    if maturity == "withdrawn" and value is not None:
        raise ValueError("withdrawn outcomes cannot carry an observation")
