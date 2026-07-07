from datetime import datetime

from pydantic import BaseModel, Field


class CapabilityPerformance(BaseModel):
    capability_id: str
    implementation_id: str | None = None
    aer: float
    risk_adjusted_return: float
    operational_drag: float
    confidence_gate_passed: bool
    confidence_breakdown: dict = Field(default_factory=dict)
    rule_output: str
    captured_at: datetime

    model_config = {"from_attributes": True}


class PerformanceSummary(BaseModel):
    capabilities: int
    avg_aer: float
    avg_risk_adjusted_return: float
    avg_operational_drag: float
