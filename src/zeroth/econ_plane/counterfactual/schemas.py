from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EvaluationMode = Literal["AB_TEST", "SHADOW", "PROXY_MODEL"]


class EvaluationRunRequest(BaseModel):
    capability_id: str
    implementation_id: str | None = None
    mode: EvaluationMode
    period_start: datetime
    period_end: datetime
    confidence_level: float = 0.95


class ValueEstimateOut(BaseModel):
    capability_id: str
    implementation_id: str | None = None
    period_start: datetime
    period_end: datetime
    estimated_value_usd: float
    estimated_cost_usd: float
    net_margin_usd: float
    credible_interval_low_usd: float
    credible_interval_high_usd: float
    confidence_level: float
    relative_interval_width: float
    confidence_gate_passed: bool
    estimation_method_version: str
    cost_data_quality: str
    value_data_quality: str
    drift_score: float
    drift_state: Literal["stable", "warning", "critical"]
    interval_method: str
    confidence_breakdown: dict = Field(default_factory=dict)
    method_metadata: dict = Field(default_factory=dict)

    model_config = {"from_attributes": True}
