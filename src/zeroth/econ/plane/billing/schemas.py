"""Normalized contracts between merchant adapters and entitlement state."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BillingSubscriptionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=32)
    event_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    external_customer_id: str = Field(min_length=1, max_length=128)
    external_subscription_id: str = Field(min_length=1, max_length=128)
    external_price_id: str = Field(min_length=1, max_length=128)
    plan: Literal["trial", "solo", "team", "scale"]
    status: Literal["trialing", "active", "past_due", "paused", "canceled"]
    period_start: datetime
    period_end: datetime
    occurred_at: datetime

    @model_validator(mode="after")
    def _period_is_forward(self) -> BillingSubscriptionEvent:
        if self.period_end <= self.period_start:
            raise ValueError("billing period end must be after its start")
        return self


class BillingSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: Literal["applied", "duplicate", "ignored_stale"]
    provider: str
    event_id: str
    tenant_id: str
