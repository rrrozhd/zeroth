"""Durable tenant-scoped economic decision history."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class EconomicDecisionRecord(Base):
    __tablename__ = "economic_decisions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.economic_decision",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index("uq_economic_decisions_tenant_digest", "tenant_id", "evidence_digest", unique=True),
        Index(
            "ix_economic_decisions_tenant_workflow_time",
            "tenant_id",
            "workflow",
            "evaluated_at",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    baseline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_type: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    evaluated_by: Mapped[str] = mapped_column(String(128), nullable=False)


class DecisionSchedule(Base):
    __tablename__ = "decision_schedules"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.decision_schedule",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index(
            "ix_decision_schedules_tenant_due",
            "tenant_id",
            "active",
            "next_run_at",
        ),
    )

    schedule_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    baseline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_type: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_decision_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
