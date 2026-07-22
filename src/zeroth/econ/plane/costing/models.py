from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Numeric, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base


class PricingCatalog(Base):
    __tablename__ = "pricing_catalog"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    region: Mapped[str] = mapped_column(String(64), default="global")
    effective_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    input_per_million_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    output_per_million_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    gpu_hour_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    cpu_hour_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    memory_gb_hour_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    energy_kwh_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)


class ToolPricingCatalog(Base):
    __tablename__ = "tool_pricing_catalog"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    unit_name: Mapped[str] = mapped_column(String(64), default="call")
    unit_rate_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    effective_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CostProfile(Base):
    __tablename__ = "cost_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    requests_per_period: Mapped[int] = mapped_column(default=0)
    avg_input_tokens: Mapped[float] = mapped_column(default=0.0)
    avg_output_tokens: Mapped[float] = mapped_column(default=0.0)
    model_mix_json: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_rate_overrides_json: Mapped[dict] = mapped_column(JSON, default=dict)
    avg_tool_calls_per_request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    tool_unit_price_overrides_json: Mapped[dict] = mapped_column(JSON, default=dict)
    infra_monthly_spend_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    retry_rate: Mapped[float] = mapped_column(default=0.0)
    cache_hit_rate: Mapped[float] = mapped_column(default=0.0)


class CostEstimate(Base):
    __tablename__ = "cost_estimates"
    __table_args__ = (Index("ix_cost_estimates_capability_period", "capability_id", "period_start"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)

    llm_cost_estimate_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    tool_cost_estimate_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    infra_cost_estimate_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    overhead_cost_estimate_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    total_cost_estimate_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)

    cost_interval_low_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    cost_interval_high_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)
    estimation_method: Mapped[str] = mapped_column(String(64), default="legacy")
    data_quality: Mapped[str] = mapped_column(String(32), default="measured")
    method_version: Mapped[str] = mapped_column(String(32), default="v1")


class GroundTruthCost(Base):
    __tablename__ = "ground_truth_costs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    component: Mapped[str] = mapped_column(String(64))
    amount_usd: Mapped[float] = mapped_column(Numeric(14, 4), default=0.0)


class CalibrationMetric(Base):
    __tablename__ = "calibration_metrics"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(32), index=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    mape: Mapped[float] = mapped_column(default=0.0)
    mae: Mapped[float] = mapped_column(default=0.0)
    rmse: Mapped[float] = mapped_column(default=0.0)
    interval_coverage: Mapped[float] = mapped_column(default=0.0)
    bias: Mapped[float] = mapped_column(default=0.0)
    sample_size: Mapped[int] = mapped_column(default=0)
