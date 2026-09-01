"""Immutable tenant-scoped hosted backtest history."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition


class EconomicBacktestRecord(Base):
    __tablename__ = "economic_backtests"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.economic_backtest",
        table_name=__tablename__,
        operations=frozenset(ResourceOperation),
    )
    __table_args__ = (
        Index("uq_economic_backtests_tenant_digest", "tenant_id", "request_digest", unique=True),
        Index("ix_economic_backtests_tenant_workflow_time", "tenant_id", "workflow", "evaluated_at"),
    )

    backtest_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    baseline_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    incumbent_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    candidate_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    provider_call_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    evaluated_by: Mapped[str] = mapped_column(String(128), nullable=False)
