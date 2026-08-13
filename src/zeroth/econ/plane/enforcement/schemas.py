from __future__ import annotations

import inspect
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["AdjustTrafficWeights", "ApplyBudgetCap", "TriggerInvestigation", "EscalateAlert"]
ActionStatus = Literal["pending", "approved", "rejected"]
PolicyStatus = Literal["PROPOSED", "APPROVED", "REJECTED", "APPLIED", "FAILED"]


class EnforcementActionCreate(BaseModel):
    capability_id: str
    action_type: ActionType
    reason: str
    before_config: dict = Field(default_factory=dict)
    after_config: dict = Field(default_factory=dict)


class EnforcementActionOut(BaseModel):
    id: int
    capability_id: str
    action_type: ActionType
    status: ActionStatus
    reason: str
    before_config: dict
    after_config: dict
    approver_sub: str | None
    approved_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PolicyActionOut(BaseModel):
    id: int
    tenant_id: str
    capability_id: str
    proposed_at: datetime
    proposed_by: str
    action_type: str
    payload_json: dict
    metrics_snapshot_id: int | None
    confidence_state_json: dict
    status: PolicyStatus
    approved_by: str | None
    approved_at: datetime | None
    applied_at: datetime | None
    failure_reason: str | None

    model_config = {"from_attributes": True}


class DecisionRequest(BaseModel):
    reason: str = ""


class TenantBudgetUpsert(BaseModel):
    budget_cap_usd: float = Field(ge=0)


class BudgetStatusOut(BaseModel):
    tenant_id: str
    total_cost_usd: float
    budget_cap_usd: float | None = None
    measurement_complete: bool = True
    cost_measurement: Literal["measured", "estimated", "unmeasured"] = "measured"
    window: str = "month_to_date"
    window_start: datetime | None = None


BudgetStatusOut.__signature__ = inspect.signature(BudgetStatusOut).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(BudgetStatusOut).parameters.items()
        if name not in {"measurement_complete", "cost_measurement"}
    ]
)
