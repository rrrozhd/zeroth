from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PricingCatalogCreate(BaseModel):
    provider: str
    model: str
    region: str = "global"
    effective_from: datetime
    effective_to: datetime | None = None
    input_per_million_usd: float = 0.0
    output_per_million_usd: float = 0.0
    gpu_hour_usd: float = 0.0
    cpu_hour_usd: float = 0.0
    memory_gb_hour_usd: float = 0.0
    energy_kwh_usd: float = 0.0


class CostProfileCreate(BaseModel):
    capability_id: str
    requests_per_period: int
    avg_input_tokens: float
    avg_output_tokens: float
    model_mix_json: dict = Field(default_factory=dict)
    provider_rate_overrides_json: dict = Field(default_factory=dict)
    avg_tool_calls_per_request_json: dict = Field(default_factory=dict)
    tool_unit_price_overrides_json: dict = Field(default_factory=dict)
    infra_monthly_spend_usd: float = 0.0
    retry_rate: float = 0.0
    cache_hit_rate: float = 0.0


class CostProfileOut(CostProfileCreate):
    id: int

    model_config = {"from_attributes": True}


class CostEstimateOut(BaseModel):
    capability_id: str
    implementation_id: str | None
    period_start: datetime
    period_end: datetime
    llm_cost_estimate_usd: float
    tool_cost_estimate_usd: float
    infra_cost_estimate_usd: float
    overhead_cost_estimate_usd: float
    total_cost_estimate_usd: float
    cost_interval_low_usd: float
    cost_interval_high_usd: float
    estimation_method: str
    data_quality: str
    method_version: str

    model_config = {"from_attributes": True}
