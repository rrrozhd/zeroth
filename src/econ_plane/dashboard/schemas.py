from pydantic import BaseModel, Field


class KPIResponse(BaseModel):
    total_ai_spend_usd: float
    total_ai_value_usd: float
    net_ai_margin_usd: float
    portfolio_confidence_score: float
    efficiency_index: float


class CapabilityValueRow(BaseModel):
    capability_id: str
    net_margin_usd: float
    confidence: float


class CapabilityRankingRow(BaseModel):
    capability_id: str
    capability_type: str
    net_margin_usd: float
    aer: float
    is_protected: bool


class ImplementationCompareRow(BaseModel):
    capability_id: str
    implementation_id: str
    arm: str | None = None
    net_margin_usd: float
    confidence_low_usd: float
    confidence_high_usd: float
    confidence_breakdown: dict = Field(default_factory=dict)


class PolicyTimelineRow(BaseModel):
    id: int
    capability_id: str
    action_type: str
    status: str
    proposed_at: str
    approved_at: str | None = None
    applied_at: str | None = None


class TrendPoint(BaseModel):
    x: str
    y: float


class DataQualityMix(BaseModel):
    measured: int
    inferred: int
    mixed: int


class ConfidenceGateStatus(BaseModel):
    passed: int
    blocked: int
