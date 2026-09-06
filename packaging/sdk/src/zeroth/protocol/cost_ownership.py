"""Explicit ownership of monetary assertions, independent of capture-layer hints."""

from decimal import Decimal
from typing import Literal

CostRole = Literal["legacy_unknown", "charge", "summary"]


def validate_cost_ownership(
    role: CostRole,
    charge_id: str | None,
    amounts: tuple[Decimal | None, ...],
) -> None:
    if role == "charge":
        if charge_id is None:
            raise ValueError("charge cost_role requires charge_id")
        charge_id.encode("utf-8")
        if any(amount is not None and amount < 0 for amount in amounts):
            raise ValueError("charge amounts must be nonnegative")
    elif charge_id is not None:
        raise ValueError("only charge cost_role can own charge_id")
    if role == "summary" and any(amount is not None for amount in amounts):
        raise ValueError("summary events cannot carry monetary amounts, including zero")
