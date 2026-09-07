"""An immutable replacement cost assertion for one existing physical charge."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChargeCostRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: str = Field(min_length=1, max_length=128)
    asserted_at: datetime
    token_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=8)
    tool_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=8)
    compute_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=8)
    cost_measurement: Literal["measured", "estimated", "unmeasured"]
    reason: str = Field(min_length=1, max_length=256)

    @field_validator(
        "token_cost_usd", "tool_cost_usd", "compute_cost_usd",
        mode="before", json_schema_input_type=str | int | None,
    )
    @classmethod
    def _exact_wire_amount(cls, value):
        if isinstance(value, float):
            raise ValueError("send fractional cost amounts as decimal strings")
        return value

    @model_validator(mode="after")
    def _assertion_contract(self) -> ChargeCostRevision:
        self.charge_id.encode("utf-8")
        if self.asserted_at.tzinfo is None or self.asserted_at.utcoffset() is None:
            raise ValueError("cost revision asserted_at requires a timezone")
        self.asserted_at = self.asserted_at.astimezone(UTC)
        present = any(value is not None for value in (
            self.token_cost_usd, self.tool_cost_usd, self.compute_cost_usd,
        ))
        if self.cost_measurement == "unmeasured" and present:
            raise ValueError("unmeasured cost revision cannot carry amounts")
        if self.cost_measurement != "unmeasured" and not present:
            raise ValueError("measured or estimated cost revision requires an amount")
        return self
