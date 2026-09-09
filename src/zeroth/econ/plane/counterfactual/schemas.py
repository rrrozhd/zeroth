from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from zeroth.econ.plane.statistics.intervals import validate_confidence

EvaluationMode = Literal["AB_TEST", "SHADOW", "PROXY_MODEL"]


class EvaluationRunRequest(BaseModel):
    capability_id: str
    implementation_id: str | None = None
    mode: EvaluationMode
    period_start: datetime
    period_end: datetime
    confidence_level: float = 0.95

    @field_validator("confidence_level")
    @classmethod
    def _confidence_is_a_probability(cls, value: float) -> float:
        # A validator rather than a ``Field`` constraint so the pinned constructor
        # signature (tests/contracts/fixtures) stays byte-identical.
        return validate_confidence(value)


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
