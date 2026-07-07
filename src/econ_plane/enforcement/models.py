from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from econ_plane.database import Base


class EnforcementAction(Base):
    __tablename__ = "enforcement_actions"
    __table_args__ = (Index("ix_enforcement_actions_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reason: Mapped[str] = mapped_column(String(512))
    before_config: Mapped[dict] = mapped_column(JSON, default=dict)
    after_config: Mapped[dict] = mapped_column(JSON, default=dict)
    approver_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PolicyAction(Base):
    __tablename__ = "policy_actions"
    __table_args__ = (Index("ix_policy_actions_tenant_status_proposed", "tenant_id", "status", "proposed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    proposed_by: Mapped[str] = mapped_column(String(128), default="system")
    action_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("performance_snapshots.id"), nullable=True)
    confidence_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED")
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class TrafficPolicy(Base):
    __tablename__ = "traffic_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)


class BudgetPolicy(Base):
    __tablename__ = "budget_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    monthly_cap_usd: Mapped[float] = mapped_column()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_sub: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
