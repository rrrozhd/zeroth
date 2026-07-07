from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from econ_plane.database import Base


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    __table_args__ = (Index("ix_performance_snapshots_capability_period", "capability_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    aer: Mapped[float] = mapped_column(Numeric(12, 4))
    risk_adjusted_return: Mapped[float] = mapped_column(Numeric(12, 4))
    operational_drag: Mapped[float] = mapped_column(Numeric(12, 4))
    confidence_gate_passed: Mapped[bool] = mapped_column(default=True)
    confidence_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_output: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
