from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CapabilityCategory = Literal["REVENUE", "COST", "RISK", "PRODUCTIVITY", "Revenue", "CostReduction", "RiskMitigation", "Productivity"]
ValuationMethod = Literal["PROXY", "AB", "SHADOW", "HYBRID"]
BaselineSource = Literal["control_arm", "historical", "external"]
ProviderType = Literal["openai", "anthropic", "custom"]
ImplementationStatus = Literal["ACTIVE", "PAUSED", "DEPRECATED"]
RoutingMode = Literal["STATIC", "AB", "SHADOW"]
ExperimentMode = Literal["AB", "SHADOW"]
AssignmentKey = Literal["request_id", "user_id"]


class ConfidenceGateConfig(BaseModel):
    min_confidence_level: float = 0.95
    max_relative_width: float = 0.30


class ValuationConfig(BaseModel):
    valuation_method: ValuationMethod = "PROXY"
    proxy_formula_id: str = "default_proxy"
    proxy_parameters: dict = Field(default_factory=dict)
    baseline_source: BaselineSource = "historical"
    confidence_gate: ConfidenceGateConfig | None = None
    cost_profile_id: int | None = None


class CapabilityCreate(BaseModel):
    id: str
    tenant_id: str | None = None
    name: str
    category: CapabilityCategory | None = None
    type: CapabilityCategory | None = None
    description: str = ""
    criticality: Literal["LOW", "MED", "HIGH"] = "MED"
    is_protected: bool = False
    valuation_config: ValuationConfig = Field(default_factory=ValuationConfig)


class CapabilityOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    type: str
    description: str
    criticality: str
    is_protected: bool
    valuation_config: ValuationConfig
    created_at: datetime

    model_config = {"from_attributes": True}


class ImplementationCreate(BaseModel):
    id: str
    tenant_id: str | None = None
    capability_id: str | None = None
    name: str
    provider: ProviderType = "custom"
    model_name: str = ""
    model_version_hash: str = ""
    prompt_version_hash: str = ""
    pipeline_version_hash: str = ""
    config_json: dict = Field(default_factory=dict)
    status: ImplementationStatus = "ACTIVE"
    model_version: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ImplementationOut(BaseModel):
    id: str
    tenant_id: str
    capability_id: str
    name: str
    provider: str
    model_name: str
    model_version_hash: str
    prompt_version_hash: str
    pipeline_version_hash: str
    config_json: dict
    status: str
    model_version: str
    created_at: datetime

    model_config = {"from_attributes": True}


class CapabilityDetail(CapabilityOut):
    implementations: list[ImplementationOut] = Field(default_factory=list)


class DeploymentCreate(BaseModel):
    tenant_id: str | None = None
    capability_id: str
    active_impl_ids: list[str] = Field(default_factory=list)
    weights_json: dict[str, float] = Field(default_factory=dict)
    routing_mode: RoutingMode = "STATIC"


class DeploymentOut(BaseModel):
    id: int
    tenant_id: str
    capability_id: str
    active_impl_ids: list[str]
    weights_json: dict[str, float]
    routing_mode: str
    updated_at: datetime


class ExperimentCreate(BaseModel):
    tenant_id: str | None = None
    capability_id: str
    mode: ExperimentMode
    impl_a_id: str
    impl_b_id: str
    start_at: datetime
    end_at: datetime | None = None
    target_pct: float = 50.0
    assignment_key: AssignmentKey = "request_id"
    status: Literal["ACTIVE", "PAUSED", "COMPLETED"] = "ACTIVE"
    constraints_json: dict = Field(default_factory=dict)


class ExperimentOut(BaseModel):
    id: int
    tenant_id: str
    capability_id: str
    mode: str
    impl_a_id: str
    impl_b_id: str
    start_at: datetime
    end_at: datetime | None
    target_pct: float
    assignment_key: str
    status: str
    constraints_json: dict

    model_config = {"from_attributes": True}
