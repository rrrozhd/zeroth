from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ_plane.database import Base


class ValuationRun(Base):
    __tablename__ = "valuation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    mode: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime] = mapped_column(DateTime)
    run_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class ValueEstimate(Base):
    __tablename__ = "value_estimates"
    __table_args__ = (Index("ix_value_estimate_capability_period", "capability_id", "period_start"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    valuation_run_id: Mapped[int] = mapped_column(ForeignKey("valuation_runs.id"), index=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)

    estimated_value_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    net_margin_usd: Mapped[float] = mapped_column(Numeric(14, 4))

    credible_interval_low_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    credible_interval_high_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    confidence_level: Mapped[float] = mapped_column(default=0.95)
    relative_interval_width: Mapped[float] = mapped_column(default=0.0)
    confidence_gate_passed: Mapped[bool] = mapped_column(default=False)
    estimation_method_version: Mapped[str] = mapped_column(String(32), default="v1")
    cost_data_quality: Mapped[str] = mapped_column(String(32), default="measured")
    value_data_quality: Mapped[str] = mapped_column(String(32), default="measured")

    drift_score: Mapped[float] = mapped_column(default=0.0)
    drift_state: Mapped[str] = mapped_column(String(32), default="stable")
    confidence_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_method: Mapped[str] = mapped_column(String(32), default="hierarchical")
    method_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
