from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_execution_events_execution_id"),
        Index("ix_execution_events_tenant_time_capability", "tenant_id", "timestamp", "capability_id"),
        Index("ix_execution_events_tenant_join_key", "tenant_id", "join_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    execution_id: Mapped[str] = mapped_column(String(128), index=True)
    join_key: Mapped[str] = mapped_column(String(128), index=True, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    implementation_id: Mapped[str] = mapped_column(String(128), index=True)
    model_version: Mapped[str] = mapped_column(String(128))
    token_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    tool_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    compute_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    cost_measurement: Mapped[str] = mapped_column(String(16), default="unmeasured")
    usage_measurement: Mapped[str] = mapped_column(String(16), default="unmeasured")
    latency_ms: Mapped[int] = mapped_column(default=0)
    compute_time_ms: Mapped[int] = mapped_column(default=0)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class OutcomeEvent(Base):
    __tablename__ = "outcome_events"
    __table_args__ = (
        Index("ix_outcome_events_tenant_time_capability", "tenant_id", "occurred_at", "capability_id"),
        Index("ix_outcome_events_tenant_join_key", "tenant_id", "join_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    join_key: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    outcome_type: Mapped[str] = mapped_column(String(64))
    outcome_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome_value: Mapped[str] = mapped_column(String(255), default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    outcome_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    provenance: Mapped[str] = mapped_column(String(16), default="MEASURED")


class EconErasureReceipt(Base):
    """Durable exactly-once receipt for tenant-scoped erasure operations."""

    __tablename__ = "econ_erasure_receipts"

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    deleted_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
